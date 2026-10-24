/**
 * Python 子进程管理器（PLAN §3.1：一个会话 = 一个 Python 子进程）。
 *
 * - spawn：`uv run --project <agentDir> python -m zcode_py`；agentDir 由扩展
 *   安装位置推导（extensionUri/../agent），无硬编码绝对路径 → 远程开发时
 *   跑在远程扩展宿主侧、用远程侧 PATH（extensionKind=workspace）。
 *   安全：spawn 用固定参数列表 + shell:false（无任何 shell 拼接），
 *   agentDir 在 spawn 前经 assertSafePath 校验（拒绝控制字符）。
 * - cwd：workspaceFolders[0].uri.fsPath，无工作区兜底 os.homedir()
 *   （vscode.d.ts：workspaceFolders 可为 undefined）。
 * - stdout：readline.createInterface 按行拆 JSONL 帧（'line' 事件天然做
 *   跨 chunk 缓冲，无尾换行不丢帧；crlfDelay:Infinity 容 CRLF）。
 *   stderr 必须消费（防管道写满阻塞），只进 OutputChannel。
 * - 关闭流程：stdin 发 EOF（协议无 shutdown 消息，Python 行循环读到 EOF
 *   自然退出）→ 3s 未退 SIGTERM → 5s 未退 SIGKILL（Windows 上信号等价
 *   强杀，Node 官方语义）。
 * - 异常退出（非人为 stop）：自动重启一次并广播 error；再崩则置 crashed
 *   不再重启（避免重启风暴）。'error' 与 'close' 可能都触发，用 settled
 *   防重入（Node 官方警告）。
 * - API Key：spawn 前经 resolveApiKeyEnv() 取「变量名→值」，合并进子进程
 *   env（必须保留 process.env 的 PATH，否则 ENOENT）。值绝不写日志。
 */

import * as vscode from "vscode";
import { spawn, ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import * as os from "node:os";
import {
  ApiKeyEnvResolution,
  CoreToHostMessage,
  HostToCoreMessage,
  InitMessage,
  encodeJsonlFrame,
  parseCoreMessage,
} from "./protocol";

/** stdin EOF 后等待进程退出的宽限，超时发 SIGTERM */
const EOF_GRACE_MS = 3_000;
/** SIGTERM 后仍未退出的硬上限，超时 SIGKILL（合计 5s，贴 deactivate 预算） */
const KILL_DEADLINE_MS = 5_000;

/**
 * 路径白名单校验：非空、无 NUL/控制字符（spawn 无 shell 本就无注入面，
 * 此校验是纵深防御——防止异常路径 smuggle 进 argv 或编码层）。
 * 返回原值便于 `const safe = assertSafePath(x)` 链式使用。
 */
export function assertSafePath(p: string, what: string): string {
  if (p.length === 0) {
    throw new Error(`${what} 为空`);
  }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(p)) {
    throw new Error(`${what} 含非法控制字符`);
  }
  return p;
}

export type SessionState = "starting" | "running" | "stopping" | "stopped" | "crashed";

export interface CoreMessageEvent {
  sessionKey: string;
  message: CoreToHostMessage;
  /** true = Python 侧新增了本镜像未知的消息类型（前向兼容放行） */
  unknownType: boolean;
}

export interface ProcessStateEvent {
  sessionKey: string;
  state: SessionState;
  detail?: string;
}

export interface ProcessErrorEvent {
  sessionKey: string;
  errorKind:
    | "agent_dir_missing"
    | "spawn_error"
    | "process_crashed"
    | "process_dead"
    | "restart_failed";
  message: string;
}

export interface SessionProcessOptions {
  sessionKey: string;
  agentDir: string;
  cwd: string;
  /** 宿主环境（含 PATH）；API Key 变量在其上合并 */
  baseEnv: NodeJS.ProcessEnv;
  init: InitMessage;
  resolveApiKeyEnv: () => Promise<ApiKeyEnvResolution>;
  outputChannel: vscode.OutputChannel;
  onMessage: (event: CoreMessageEvent) => void;
  onState: (event: ProcessStateEvent) => void;
  onError: (event: ProcessErrorEvent) => void;
}

/** 单会话子进程：生命周期、帧收发、优雅退出、一次自动重启 */
class SessionProcess {
  public readonly sessionKey: string;
  private readonly opts: SessionProcessOptions;
  private child?: ChildProcess;
  private _state: SessionState = "starting";
  /** 'error' 与 'close' 双触发防重入锚点 */
  private settled = false;
  private manualStop = false;
  /** 终态标志：stop() 后不再允许 spawnChild 复活（防 stop 与自动重启竞态泄漏进程） */
  private terminated = false;
  private restartedOnce = false;
  private graceTimer?: NodeJS.Timeout;
  private killTimer?: NodeJS.Timeout;
  private stoppedResolve?: () => void;
  private stoppedPromise: Promise<void> = Promise.resolve();

  constructor(opts: SessionProcessOptions) {
    this.sessionKey = opts.sessionKey;
    this.opts = opts;
  }

  public get state(): SessionState {
    return this._state;
  }

  /** 启动子进程（幂等：已启动/已终止直接返回） */
  public async start(): Promise<void> {
    if (this.terminated || this.child !== undefined || this._state === "stopped" || this._state === "crashed") {
      return;
    }
    await this.spawnChild();
  }

  private async spawnChild(): Promise<void> {
    if (this.terminated) {
      this.setState("stopped");
      return;
    }
    this.settled = false;
    this.manualStop = false;
    this.stoppedPromise = new Promise<void>((resolve) => {
      this.stoppedResolve = resolve;
    });

    // API Key 每次启动时现取（改 Key 无需重启扩展）；值只进子进程 env
    let apiKeyValues: Record<string, string> = {};
    try {
      apiKeyValues = (await this.opts.resolveApiKeyEnv()).values;
    } catch (err) {
      this.opts.outputChannel.appendLine(
        `[${this.sessionKey}] resolve api key env failed: ${String(err)}（继续无 Key 启动）`,
      );
    }
    const env: NodeJS.ProcessEnv = { ...this.opts.baseEnv, ...apiKeyValues };

    // await 窗口内可能已 stop()：二次确认终态，避免复活已停会话
    if (this.terminated || this.manualStop) {
      this.setState("stopped");
      return;
    }

    const agentDir = assertSafePath(this.opts.agentDir, "agentDir");
    const cwd = assertSafePath(this.opts.cwd, "cwd");
    // 固定参数列表直传 argv；shell:false 绝不经 shell 解释
    const child = spawn("uv", ["run", "--project", agentDir, "python", "-m", "zcode_py"], {
      cwd,
      env,
      windowsHide: true,
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;

    // stdin 写入端可能 EPIPE（进程已死）——必须挂 error，否则 unhandled 'error'
    child.stdin?.on("error", () => {
      /* 状态迁移统一由 close/error 事件处理 */
    });

    // stdout 按行拆帧：readline 的 'line' 事件自带跨 chunk 缓冲
    const stdout = createInterface({ input: child.stdout, crlfDelay: Infinity });
    stdout.on("line", (line) => this.handleLine(line));

    // stderr 必须消费，否则子进程写满管道后阻塞；只进日志不入 UI
    const stderr = createInterface({ input: child.stderr, crlfDelay: Infinity });
    stderr.on("line", (line) => {
      if (line.trim().length > 0) {
        this.opts.outputChannel.appendLine(`[${this.sessionKey}][py-stderr] ${line}`);
      }
    });

    child.once("spawn", () => {
      this.setState("running");
      // 启动握手：init 是宿主发给核心的第一条消息
      this.send(this.opts.init);
    });
    child.once("error", (err) => this.handleSpawnFailure(err));
    child.once("close", (code, signal) => this.handleClose(code, signal));
  }

  private handleLine(line: string): void {
    const parsed = parseCoreMessage(line);
    if (!parsed.ok) {
      // 单帧降级：协议外输出只记日志（可能是 Python 侧 print 调试残留）
      this.opts.outputChannel.appendLine(
        `[${this.sessionKey}][py-stdout 丢弃非协议帧] ${parsed.error}: ${parsed.raw.slice(0, 400)}`,
      );
      return;
    }
    if (!parsed.knownType) {
      this.opts.outputChannel.appendLine(
        `[${this.sessionKey}][py-stdout 未知类型 ${(parsed.message as { type: string }).type}，前向放行]`,
      );
    }
    this.opts.onMessage({
      sessionKey: this.sessionKey,
      message: parsed.message,
      unknownType: !parsed.knownType,
    });
  }

  /** 发一条消息给核心；进程不在（未启动/已死）返回 false */
  public send(message: HostToCoreMessage): boolean {
    const stdin = this.child?.stdin;
    if (stdin === undefined || stdin === null || stdin.destroyed || stdin.writableEnded) {
      return false;
    }
    try {
      stdin.write(encodeJsonlFrame(message));
      return true;
    } catch {
      return false;
    }
  }

  private setState(state: SessionState, detail?: string): void {
    if (this._state === state && detail === undefined) {
      return;
    }
    this._state = state;
    this.opts.onState({ sessionKey: this.sessionKey, state, detail });
  }

  private handleSpawnFailure(err: Error): void {
    if (this.settled) {
      return;
    }
    this.settled = true;
    this.clearTimers();
    this.setState("crashed", `spawn failed: ${err.message}`);
    this.opts.onError({
      sessionKey: this.sessionKey,
      errorKind: "spawn_error",
      message: `无法启动 uv 子进程: ${err.message}`,
    });
    this.stoppedResolve?.();
  }

  private handleClose(code: number | null, signal: string | null): void {
    if (this.settled) {
      return;
    }
    this.settled = true;
    this.clearTimers();
    const detail = `exit=${code ?? "?"} signal=${signal ?? "?"}`;

    if (this.manualStop || this.terminated) {
      this.setState("stopped", detail);
      this.stoppedResolve?.();
      return;
    }

    // 异常退出：广播错误 → 自动重启一次 → 再崩放弃
    this.opts.onError({
      sessionKey: this.sessionKey,
      errorKind: "process_crashed",
      message: `Python 核心异常退出（${detail}）`,
    });
    if (this.restartedOnce) {
      this.setState("crashed", detail);
      this.opts.onError({
        sessionKey: this.sessionKey,
        errorKind: "process_dead",
        message: `Python 核心二次崩溃（${detail}），已停止自动重启，请手动新建会话`,
      });
      this.stoppedResolve?.();
      return;
    }
    this.restartedOnce = true;
    this.setState("starting", `重启中（上次 ${detail}）`);
    this.child = undefined;
    this.spawnChild().catch((err) => {
      this.setState("crashed", `restart failed: ${String(err)}`);
      this.opts.onError({
        sessionKey: this.sessionKey,
        errorKind: "restart_failed",
        message: `自动重启失败: ${String(err)}`,
      });
      this.stoppedResolve?.();
    });
  }

  /** 优雅停止：stdin EOF → SIGTERM → SIGKILL；幂等，可重复调用 */
  public async stop(): Promise<void> {
    this.manualStop = true;
    this.terminated = true;
    const child = this.child;
    if (child === undefined || this.settled) {
      this.setState("stopped");
      return;
    }
    this.setState("stopping");
    try {
      child.stdin?.end();
    } catch {
      /* 已死则交给 close 事件 */
    }
    this.graceTimer = setTimeout(() => {
      if (!this.settled) {
        child.kill("SIGTERM");
      }
    }, EOF_GRACE_MS);
    this.killTimer = setTimeout(() => {
      if (!this.settled) {
        child.kill("SIGKILL");
      }
    }, KILL_DEADLINE_MS);
    await this.stoppedPromise;
  }

  private clearTimers(): void {
    if (this.graceTimer !== undefined) {
      clearTimeout(this.graceTimer);
      this.graceTimer = undefined;
    }
    if (this.killTimer !== undefined) {
      clearTimeout(this.killTimer);
      this.killTimer = undefined;
    }
  }

  public dispose(): void {
    void this.stop();
  }
}

export interface PythonProcessManagerOptions {
  /** 由扩展位置推导的 agent 目录绝对路径（远程下即远程侧路径） */
  agentDir: string;
  outputChannel: vscode.OutputChannel;
  /** SecretStorage → 环境变量解析（spawn 时现取） */
  resolveApiKeyEnv: () => Promise<ApiKeyEnvResolution>;
}

/** 多会话管理：每会话一个子进程（R16 并行多开）；deactivate 全量回收 */
export class PythonProcessManager implements vscode.Disposable {
  private readonly sessions = new Map<string, SessionProcess>();
  private readonly opts: PythonProcessManagerOptions;
  private nextSeq = 1;

  private readonly messageEmitter = new vscode.EventEmitter<CoreMessageEvent>();
  private readonly stateEmitter = new vscode.EventEmitter<ProcessStateEvent>();
  private readonly errorEmitter = new vscode.EventEmitter<ProcessErrorEvent>();
  public readonly onCoreMessage = this.messageEmitter.event;
  public readonly onProcessState = this.stateEmitter.event;
  public readonly onProcessError = this.errorEmitter.event;

  constructor(opts: PythonProcessManagerOptions) {
    this.opts = opts;
  }

  /** 子进程 cwd：首个工作区根；无工作区窗口兜底 home（d.ts：可为 undefined） */
  public static defaultCwd(): string {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? os.homedir();
  }

  /** init.workspaceRoots：多根工作区全量（远程下 fsPath 即宿主侧真实路径） */
  public static workspaceRoots(): string[] {
    return vscode.workspace.workspaceFolders?.map((f) => f.uri.fsPath) ?? [];
  }

  /** 新建会话并 spawn（sessionKey 缺省自动生成）；agentDir 缺失时广播错误并返回 undefined */
  public async createSession(sessionKey?: string): Promise<SessionProcess | undefined> {
    const key = sessionKey ?? `s${Date.now().toString(36)}-${this.nextSeq++}`;
    if (this.sessions.has(key)) {
      return this.sessions.get(key);
    }
    try {
      await vscode.workspace.fs.stat(vscode.Uri.file(this.opts.agentDir));
    } catch {
      this.errorEmitter.fire({
        sessionKey: key,
        errorKind: "agent_dir_missing",
        message: `agent 目录不存在: ${this.opts.agentDir}（开发模式需在仓库内 F5；打包分发另见 PLAN §4）`,
      });
      return undefined;
    }
    const resolution = await this.opts.resolveApiKeyEnv();
    const init: InitMessage = {
      type: "init",
      workspaceRoots: PythonProcessManager.workspaceRoots(),
      settings: {},
      apiKeyEnv: resolution.mapping,
    };
    const proc = new SessionProcess({
      sessionKey: key,
      agentDir: this.opts.agentDir,
      cwd: PythonProcessManager.defaultCwd(),
      baseEnv: process.env,
      init,
      resolveApiKeyEnv: this.opts.resolveApiKeyEnv,
      outputChannel: this.opts.outputChannel,
      onMessage: (e) => this.messageEmitter.fire(e),
      onState: (e) => this.stateEmitter.fire(e),
      onError: (e) => this.errorEmitter.fire(e),
    });
    this.sessions.set(key, proc);
    await proc.start();
    return proc;
  }

  public get(sessionKey: string): SessionProcess | undefined {
    return this.sessions.get(sessionKey);
  }

  public sessionKeys(): string[] {
    return [...this.sessions.keys()];
  }

  public async stopSession(sessionKey: string): Promise<void> {
    const proc = this.sessions.get(sessionKey);
    if (proc === undefined) {
      return;
    }
    this.sessions.delete(sessionKey);
    await proc.stop();
  }

  /** deactivate 用：全量优雅回收（EOF→SIGTERM→SIGKILL，总预算约 5s） */
  public async stopAll(): Promise<void> {
    const procs = [...this.sessions.values()];
    this.sessions.clear();
    await Promise.allSettled(procs.map((p) => p.stop()));
  }

  public dispose(): void {
    void this.stopAll();
    this.messageEmitter.dispose();
    this.stateEmitter.dispose();
    this.errorEmitter.dispose();
  }
}
