import type { ReactNode } from "react";
import type { Credential, TosRisk } from "../../api/types";
import { AddCredentialDialog } from "../../components/AddCredentialDialog";
import { BatchImportDialog } from "../../components/BatchImportDialog";
import { CredentialLimitsDialog } from "../../components/CredentialLimitsDialog";
import { CredentialLoginSecretDialog } from "../../components/CredentialLoginSecretDialog";
import { CredentialTagsDialog } from "../../components/CredentialTagsDialog";
import { CredentialWebCookieDialog } from "../../components/CredentialWebCookieDialog";
import { CredentialWeightDialog } from "../../components/CredentialWeightDialog";

/**
 * 凭证页的对话框组：限额 / 标签 / 权重 / 账号密码 / 网页 Cookie / 批量导入 /
 * 添加凭证。打开态与刷新回调都由页面壳持有，这里只按条件渲染，
 * 关闭与保存后的刷新文案一律沿用原实现。
 */
export function CredentialDialogs({
  canWrite,
  limitsCredential,
  setLimitsFor,
  tagsCredential,
  setTagsFor,
  weightCredential,
  setWeightFor,
  loginSecretCredential,
  setLoginSecretFor,
  webCookieCredential,
  setWebCookieFor,
  batchOpen,
  setBatchOpen,
  addOpen,
  setAddOpen,
  credentialCount,
  hasZen,
  hasKilo,
  lingxiCount,
  tosRisks,
  showNotice,
  showError,
  refresh,
}: {
  canWrite: boolean;
  limitsCredential: Credential | null;
  setLimitsFor: (id: string | null) => void;
  tagsCredential: Credential | null;
  setTagsFor: (id: string | null) => void;
  weightCredential: Credential | null;
  setWeightFor: (id: string | null) => void;
  loginSecretCredential: Credential | null;
  setLoginSecretFor: (id: string | null) => void;
  webCookieCredential: Credential | null;
  setWebCookieFor: (id: string | null) => void;
  batchOpen: boolean;
  setBatchOpen: (open: boolean) => void;
  addOpen: boolean;
  setAddOpen: (open: boolean) => void;
  credentialCount: number;
  hasZen: boolean;
  hasKilo: boolean;
  lingxiCount: number;
  tosRisks: Record<string, TosRisk>;
  showNotice: (content: ReactNode) => void;
  showError: (content: ReactNode) => void;
  refresh: () => Promise<unknown>;
}) {
  return (
    <>
      {canWrite && limitsCredential && (
        <CredentialLimitsDialog
          credential={limitsCredential}
          onClose={() => setLimitsFor(null)}
          onSaved={(message) => {
            setLimitsFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && tagsCredential && (
        <CredentialTagsDialog
          credential={tagsCredential}
          onClose={() => setTagsFor(null)}
          onSaved={(message) => {
            setTagsFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && weightCredential && (
        <CredentialWeightDialog
          credential={weightCredential}
          onClose={() => setWeightFor(null)}
          onSaved={(message) => {
            setWeightFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && loginSecretCredential && (
        <CredentialLoginSecretDialog
          credential={loginSecretCredential}
          onClose={() => setLoginSecretFor(null)}
          onSaved={(message) => {
            setLoginSecretFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && webCookieCredential && (
        <CredentialWebCookieDialog
          credential={webCookieCredential}
          onClose={() => setWebCookieFor(null)}
          onSaved={(message) => {
            setWebCookieFor(null);
            showNotice(message);
            void refresh();
          }}
          onError={(message) => showError(message)}
        />
      )}

      {canWrite && batchOpen && (
        <BatchImportDialog
          onClose={() => setBatchOpen(false)}
          onImported={(message) => {
            showNotice(message);
            void refresh();
          }}
          onNotice={showNotice}
        />
      )}

      {canWrite && (
        <AddCredentialDialog
          open={addOpen}
          onClose={() => setAddOpen(false)}
          credentialCount={credentialCount}
          hasZen={hasZen}
          hasKilo={hasKilo}
          lingxiCount={lingxiCount}
          tosRisks={tosRisks}
          onImported={(message) => {
            setAddOpen(false);
            showNotice(message);
            void refresh();
          }}
          onNotice={showNotice}
          onError={showError}
        />
      )}
    </>
  );
}
