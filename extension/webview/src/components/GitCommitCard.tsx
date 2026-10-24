/**
 * GitCommitCard 一键提交卡（R17 学习式提交 + R22 推送/PR，PLAN §2.6）：
 *  - control git_preview{repoRoot, scope} → diff 统计 + 按仓库风格生成的建议
 *    提交信息（store.gitPreview；风格学习在 agent/zcode_py/gitflow.py：
 *    git log 最近 50 条 subject 启发式归纳 conventional/语言/句号习惯）
 *  - 仓库选择（multi-root 下拉，workspaceRoots 数据源）/ 范围单选
 *    staged|all / 推送勾选（勾选即「提交并推送」，凭据走系统
 *    credential-helper，插件不经手 token——PLAN §9）
 *  - 建议信息可编辑（subject + 空行 + body 单文本域；确认即 git_commit）
 *  - git_result：sha、可点 commit 网址（remote 规范化 + sha 拼接）、「创建
 *    PR」按钮（gh 在 PATH 且 GitHub remote；gh pr create --fill 用刚提交的
 *    message）→ PR 网址
 *  - 无 git 仓库 / 无 remote / 非 GitHub / gh 缺失：降级文案（不弹错误进
 *    聊天流——错误只在本卡展示）
 */
import { useEffect, useRef, useState } from 'react';
import { useStore } from '../store/store';
import { useT } from '../i18n';
import { Icon } from './Icon';

const MAX_FILES_SHOWN = 8;

const shortSha = (sha: string): string => (sha.length > 10 ? sha.slice(0, 10) : sha);

export function GitCommitCard(): React.JSX.Element {
  const gitRepos = useStore((s) => s.gitRepos);
  const gitRepoRoot = useStore((s) => s.gitRepoRoot);
  const gitScope = useStore((s) => s.gitScope);
  const gitPush = useStore((s) => s.gitPush);
  const gitPreview = useStore((s) => s.gitPreview);
  const gitPreviewLoading = useStore((s) => s.gitPreviewLoading);
  const gitBusy = useStore((s) => s.gitBusy);
  const gitResult = useStore((s) => s.gitResult);
  const setGitRepo = useStore((s) => s.setGitRepo);
  const setGitScope = useStore((s) => s.setGitScope);
  const setGitPush = useStore((s) => s.setGitPush);
  const requestGitPreview = useStore((s) => s.requestGitPreview);
  const sendGitCommit = useStore((s) => s.sendGitCommit);
  const sendGitPr = useStore((s) => s.sendGitPr);
  const closeGitPanel = useStore((s) => s.closeGitPanel);
  const t = useT();

  // 建议信息草稿：每次新预览到达（repoRoot/scope 变化或手动刷新）重置为
  // subject + 空行 + body；此后归用户编辑（确认时原样发回核侧）
  const [draft, setDraft] = useState('');
  const previewKeyRef = useRef('');
  useEffect(() => {
    if (!gitPreview || !gitPreview.ok) return;
    const key = `${gitPreview.repoRoot}::${gitPreview.scope}::${gitPreview.subject}`;
    if (previewKeyRef.current === key) return;
    previewKeyRef.current = key;
    setDraft(
      gitPreview.body !== '' ? `${gitPreview.subject}\n\n${gitPreview.body}` : gitPreview.subject,
    );
  }, [gitPreview]);

  // 预览驱动：repoRoot/scope 就绪且尚无本组合的预览时自动拉一次（面板
  // 打开即见统计与建议；切换仓库/范围重拉——store 换组合时清空 gitPreview）
  useEffect(() => {
    if (gitRepoRoot !== undefined && gitRepoRoot !== '' && !gitPreviewLoading && gitPreview === undefined) {
      requestGitPreview();
    }
  }, [gitRepoRoot, gitScope, gitPreview, gitPreviewLoading, requestGitPreview]);

  const files = gitPreview?.files ?? [];
  const canCommit = gitPreview?.ok === true && files.length > 0 && !gitBusy && draft.trim() !== '';

  const onConfirm = (): void => {
    // 确认：推送与否按勾选（store.sendGitCommit 缺省读 gitPush）
    sendGitCommit(draft);
  };

  const onCommitOnly = (): void => {
    // 仅提交：强制不推送（不改动推送勾选——下次确认仍按勾选走）
    sendGitCommit(draft, false);
  };

  return (
    <section className="gitpanel" aria-label={t('git.panelAria')}>
      <div className="gitpanel-head">
        <span className="telegraph-eyebrow">GIT</span>
        <span className="telegraph-title">一键 Commit &amp; Push</span>
        <span className="muted gitpanel-note" title={gitPreview?.styleNote}>
          {gitPreview?.styleNote !== '' && gitPreview?.styleNote !== undefined
            ? gitPreview.styleNote
            : t('git.styleNoteFallback')}
        </span>
        <button
          className="iconbtn"
          onClick={closeGitPanel}
          title={t('git.collapse')}
          aria-label={t('git.collapse')}
        >
          <Icon name="close" />
        </button>
      </div>

      {gitRepos.length === 0 ? (
        <div className="gitpanel-body muted">{t('git.noRoots')}</div>
      ) : (
        <>
          <div className="gitpanel-row">
            {gitRepos.length > 1 && (
              <label className="ctl-permission" title={t('git.repoTitle')}>
                <Icon name="file" size={12} />
                <select
                  className="sel"
                  value={gitRepoRoot ?? ''}
                  onChange={(e) => setGitRepo(e.target.value)}
                  aria-label={t('git.repoAria')}
                >
                  {gitRepos.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </label>
            )}
            <div className="gitpanel-scope" role="radiogroup" aria-label={t('git.scopeAria')}>
              <label title={t('git.stagedTitle')}>
                <input
                  type="radio"
                  name="git-scope"
                  checked={gitScope === 'staged'}
                  onChange={() => setGitScope('staged')}
                />
                {t('git.staged')}
              </label>
              <label title={t('git.allTitle')}>
                <input
                  type="radio"
                  name="git-scope"
                  checked={gitScope === 'all'}
                  onChange={() => setGitScope('all')}
                />
                {t('git.all')}
              </label>
            </div>
            <label className="gitpanel-push" title={t('git.pushTitle')}>
              <input
                type="checkbox"
                checked={gitPush}
                onChange={(e) => setGitPush(e.target.checked)}
              />
              {t('git.push')}
            </label>
            <button
              className="quickbtn"
              onClick={() => requestGitPreview()}
              disabled={gitPreviewLoading || gitRepoRoot === undefined}
              title={t('git.refreshTitle')}
            >
              <Icon name="sparkles" size={12} />{gitPreviewLoading ? t('git.refreshing') : t('git.refreshBtn')}
            </button>
          </div>

          {gitPreview === undefined || gitPreviewLoading ? (
            <div className="gitpanel-body muted">{t('git.loading')}</div>
          ) : gitPreview.ok ? (
            <>
              <div className="gitpanel-stats">
                <span title={t('git.filesTitle', { n: files.length, untracked: gitPreview.untracked })}>
                  {t('git.filesLabel', { n: files.length })}
                </span>
                <span className="add">+{gitPreview.insertions}</span>
                <span className="del">−{gitPreview.deletions}</span>
                {gitPreview.untracked > 0 && (
                  <span className="muted">{t('git.untrackedCount', { n: gitPreview.untracked })}</span>
                )}
              </div>
              <ul className="gitpanel-files">
                {files.slice(0, MAX_FILES_SHOWN).map((f) => (
                  <li key={f.path} title={f.path}>
                    <span className="gitpanel-status">{f.status}</span>
                    <span className="gitpanel-path">{f.path}</span>
                    {(f.insertions !== null || f.deletions !== null) && (
                      <span className="gitpanel-counts">
                        <span className="add">+{f.insertions ?? 0}</span>
                        <span className="del">−{f.deletions ?? 0}</span>
                      </span>
                    )}
                  </li>
                ))}
                {files.length > MAX_FILES_SHOWN && (
                  <li className="muted">{t('git.moreFiles', { n: files.length - MAX_FILES_SHOWN })}</li>
                )}
                {files.length === 0 && <li className="muted">{t('git.noChanges')}</li>}
              </ul>
              <textarea
                className="in gitpanel-msg"
                rows={5}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={t('git.msgPlaceholder')}
                title={t('git.msgTitle')}
                aria-label={t('git.msgAria')}
              />
              <div className="gitpanel-actions">
                <button
                  className="btn"
                  disabled={!canCommit}
                  onClick={onConfirm}
                  title={gitPush ? t('git.confirmPushTitle') : t('git.confirmTitle')}
                >
                  <Icon name="commit" size={12} />
                  {gitPush ? t('git.confirmWithPush') : t('git.confirmOnly')}
                </button>
                <button
                  className="btn sec"
                  disabled={!canCommit}
                  onClick={onCommitOnly}
                  title={t('git.commitOnlyTitle')}
                >
                  {t('git.commitOnly')}
                </button>
                <button className="btn sec" onClick={closeGitPanel} title={t('git.closeTitle')}>
                  {t('git.cancel')}
                </button>
              </div>
            </>
          ) : (
            <div className="gitpanel-body">
              <span className="bad" title={gitPreview.error ?? ''}>
                <Icon name="warning" size={12} /> {gitPreview.error ?? t('git.previewFailed')}
              </span>
              <button
                className="quickbtn"
                onClick={() => requestGitPreview()}
                disabled={gitPreviewLoading}
                title={t('git.retry')}
              >
                {t('git.retry')}
              </button>
            </div>
          )}

          {gitResult !== undefined && (
            <div className="gitpanel-result">
              {gitResult.error !== undefined && gitResult.error !== '' && (
                <div className="bad" title={gitResult.error}>
                  <Icon name="warning" size={12} /> {gitResult.error}
                </div>
              )}
              {gitResult.hint !== undefined && gitResult.hint !== '' && (
                <div className="muted" title={gitResult.hint}>{gitResult.hint}</div>
              )}
              {gitResult.commitSha !== undefined && gitResult.commitSha !== '' && (
                <div>
                  {t('git.committed')} <code>{shortSha(gitResult.commitSha)}</code>
                  {gitResult.commitUrl !== undefined && (
                    <>
                      {' '}
                      <a href={gitResult.commitUrl} target="_blank" rel="noreferrer" title={gitResult.commitUrl}>
                        {t('git.viewCommit')}
                      </a>
                    </>
                  )}
                </div>
              )}
              {gitResult.prUrl !== undefined && gitResult.prUrl !== '' && (
                <div>
                  <a href={gitResult.prUrl} target="_blank" rel="noreferrer" title={gitResult.prUrl}>
                    {t('git.prCreated')}
                  </a>
                </div>
              )}
              {gitResult.prUrl === undefined
                && gitResult.error === undefined
                && gitResult.commitSha !== undefined
                && gitResult.commitSha !== ''
                && gitPreview?.canPr === true && (
                <button
                  className="btn sec"
                  disabled={gitBusy}
                  onClick={() => sendGitPr()}
                  title={t('git.createPrTitle')}
                >
                  {t('git.createPr')}
                </button>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
