/**
 * 前端与后端共享的常量（值必须与后端保持一致，改一处要同步改另一处）。
 */

/** 密码最短长度。与后端 `src/api/admin_auth.py` 的 `MIN_PASSWORD_LENGTH` 一致。 */
export const MIN_PASSWORD_LENGTH = 8;

/**
 * 全站统一的分页每页条数选项。
 *
 * 所有列表页共用同一组：用户不该在「用量统计」看到 10/20/50/100、在「模型列表」
 * 又看到 20/50/100。默认值取 `DEFAULT_PAGE_SIZE`（见下），各页可显式覆盖。
 */
export const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;

/** 默认每页条数（`PAGE_SIZE_OPTIONS` 中的一项）。 */
export const DEFAULT_PAGE_SIZE = 50;
