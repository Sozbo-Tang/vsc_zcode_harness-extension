/**
 * API Key 存取（PLAN §3.2：密钥只进 VS Code SecretStorage，键名
 * `provider:{id}:apikey`；永不落盘明文、永不出现在日志/转录/协议消息里）。
 *
 * 给子进程的方式：spawn 时注入环境变量 `ZCODE_PY_API_KEY_<SANITIZED_ID>`
 * （Python 侧 config.py 只认环境变量，providers.json 不含 Key）。
 */

import * as vscode from "vscode";
import { ApiKeyEnvResolution } from "./protocol";

const SECRET_PREFIX = "provider:";
const SECRET_SUFFIX = ":apikey";
const ENV_PREFIX = "ZCODE_PY_API_KEY_";

/** SecretStorage 键名：provider:{id}:apikey */
export function secretKeyFor(providerId: string): string {
  return `${SECRET_PREFIX}${providerId}${SECRET_SUFFIX}`;
}

/** 从 SecretStorage 键名反解 providerId（非本前缀返回 undefined） */
export function providerIdFromSecretKey(key: string): string | undefined {
  if (!key.startsWith(SECRET_PREFIX) || !key.endsWith(SECRET_SUFFIX)) {
    return undefined;
  }
  const id = key.slice(SECRET_PREFIX.length, key.length - SECRET_SUFFIX.length);
  return id.length > 0 ? id : undefined;
}

/**
 * providerId → 环境变量名（大写，非字母数字下划线折叠为 `_`）。
 * 注意：`a-b` 与 `a_b` 会折叠到同名变量（M1 已知限制，冲突时后写覆盖并
 * 由调用方告警——只告警变量名，不涉及值）。
 */
export function apiKeyEnvName(providerId: string): string {
  return `${ENV_PREFIX}${providerId.toUpperCase().replace(/[^A-Z0-9_]/g, "_")}`;
}

export class ApiKeyStore {
  constructor(private readonly secrets: vscode.SecretStorage) {}

  /** 读某 Provider 的 Key（不存在返回 undefined） */
  public async get(providerId: string): Promise<string | undefined> {
    return this.secrets.get(secretKeyFor(providerId));
  }

  /** 存 Key（SecretStorage 平台加密存储，不跨机器同步） */
  public async set(providerId: string, value: string): Promise<void> {
    await this.secrets.store(secretKeyFor(providerId), value);
  }

  public async delete(providerId: string): Promise<void> {
    await this.secrets.delete(secretKeyFor(providerId));
  }

  /** 列出已存 Key 的 providerId 列表 */
  public async listProviderIds(): Promise<string[]> {
    const keys = await this.secrets.keys();
    const ids: string[] = [];
    for (const key of keys) {
      const id = providerIdFromSecretKey(key);
      if (id !== undefined) {
        ids.push(id);
      }
    }
    return ids;
  }

  /**
   * 汇总 spawn 需要的两张表：
   * mapping 给 init 消息（providerId → 变量名，无值）；
   * values 给子进程环境（变量名 → 值）。值只进子进程 env，不进任何日志。
   */
  public async resolve(): Promise<ApiKeyEnvResolution> {
    const mapping: Record<string, string> = {};
    const values: Record<string, string> = {};
    for (const id of await this.listProviderIds()) {
      const value = await this.secrets.get(secretKeyFor(id));
      if (!value) {
        continue;
      }
      const name = apiKeyEnvName(id);
      mapping[id] = name;
      values[name] = value;
    }
    return { mapping, values };
  }
}
