/**
 * 设置页默认值常量（R15/R20）。
 *
 * DEFAULT_PRIVACY_PATTERNS 镜像 agent/zcode_py/privacy.py 的
 * PRIVACY_BASENAME_PATTERNS（设置页「恢复默认」按钮数据源）。双侧手写镜像
 * ——隐私护栏是警告型防线，核心侧才是真源；这里漂移只会让「恢复默认」
 * 恢复到旧清单，不会放掉保护（核心消费点另有路径子串/URL 后缀内置兜底）。
 */
export const DEFAULT_PRIVACY_PATTERNS: string[] = [
  '.env',
  '.env.*',
  '*.env',
  '*.pem',
  '*.key',
  '*.p12',
  '*.pfx',
  '*.jks',
  '*.keystore',
  'id_rsa',
  'id_rsa*',
  'id_dsa*',
  'id_ecdsa*',
  'id_ed25519*',
  '*.kdbx',
  'credentials*',
  '*.credentials',
  '.npmrc',
  '.netrc',
  '.pypirc',
  'secrets.*',
  'secret.key',
];
