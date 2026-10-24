/**
 * 车钟咔哒音效（R6 收尾 / PLAN §6.2）：换挡吸附瞬间的短促机械咔哒。
 *
 * 设计决策：
 * - **纯 WebAudio 合成，零音频资源文件**：短噪声突发（碰撞瞬态，模拟
 *   棘爪打在挡位棘轮上的「嗒」）+ 双振荡器快衰减包络（高频金属体 +
 *   低频机械体），总时长 ~30ms——不打包任何 wav/mp3，无加载时延。
 * - **惰性创建 AudioContext**：浏览器 autoplay 策略要求音频上下文在
 *   用户手势内创建/resume；首次调用发生在车钟 pointer-up（真实手势）
 *   的 commit 路径上，此前模块不持有任何音频资源。
 * - **静默降级**：AudioContext 不存在 / 创建或调度抛异常（策略拦截、
 *   节点耗尽等）只置失败标记后直接返回，绝不向 UI 冒错——音效纯装饰，
 *   不影响换挡功能。
 * - **低音量**：master gain 0.12（≤ 0.15 上限），咔哒只是触觉级暗示。
 * - 开关由设置页「音效」组控制（核心 settings 通道键
 *   telegraphClickSound，默认开）；判定在调用方（Telegraph commit 路径）
 *   现读 store，本模块不依赖 store。
 */

/** 总音量（低电平；PLAN §6.2 要求音效可关且不扰，上限 0.15） */
const CLICK_GAIN = 0.12;

let ctx: AudioContext | null = null;
/** AudioContext 创建失败后不再重试（避免每次换挡重复抛错/开销） */
let failed = false;

function ensureContext(): AudioContext | null {
  if (failed) return null;
  if (ctx !== null) return ctx;
  try {
    if (typeof AudioContext === 'undefined') throw new Error('AudioContext unavailable');
    ctx = new AudioContext();
  } catch {
    failed = true;
    return null;
  }
  return ctx;
}

/**
 * 换挡吸附咔哒：① 4ms 噪声瞬态过 1.8kHz 高通指数衰减（~6ms）；
 * ② 1.9kHz 方波 + 310Hz 方波双振荡包络（8ms / 24ms 衰减）。
 * 调用点在用户手势内（pointer-up commit）。
 */
export function playTelegraphClick(): void {
  const ac = ensureContext();
  if (ac === null) return;
  try {
    // suspended（策略/后台）时借本次手势唤醒；唤醒失败不响不报错
    if (ac.state === 'suspended') void ac.resume().catch(() => {});
    const t0 = ac.currentTime;

    const master = ac.createGain();
    master.gain.value = CLICK_GAIN;
    master.connect(ac.destination);

    // ① 噪声瞬态：随机样本 → 高通 → 指数衰减，金属碰撞的「嗒」
    const noiseLen = Math.max(1, Math.floor(ac.sampleRate * 0.004));
    const buf = ac.createBuffer(1, noiseLen, ac.sampleRate);
    const data = buf.getChannelData(0);
    for (let i = 0; i < noiseLen; i++) data[i] = Math.random() * 2 - 1;
    const noise = ac.createBufferSource();
    noise.buffer = buf;
    const hp = ac.createBiquadFilter();
    hp.type = 'highpass';
    hp.frequency.value = 1800;
    const noiseGain = ac.createGain();
    noiseGain.gain.setValueAtTime(1, t0);
    noiseGain.gain.exponentialRampToValueAtTime(0.001, t0 + 0.006);
    noise.connect(hp).connect(noiseGain).connect(master);
    noise.start(t0);
    noise.stop(t0 + 0.008);

    // ② 双振荡包络：方波富含谐波更像棘轮；高频亮、低频沉
    const layers = [
      { freq: 1900, level: 0.5, decay: 0.008 },
      { freq: 310, level: 0.35, decay: 0.024 },
    ] as const;
    for (const { freq, level, decay } of layers) {
      const osc = ac.createOscillator();
      osc.type = 'square';
      osc.frequency.value = freq;
      const g = ac.createGain();
      g.gain.setValueAtTime(level, t0);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + decay);
      osc.connect(g).connect(master);
      osc.start(t0);
      osc.stop(t0 + decay + 0.005);
    }
  } catch {
    // 调度失败极罕见：本次不响即可，不标记 failed（下次点击重试）
  }
}
