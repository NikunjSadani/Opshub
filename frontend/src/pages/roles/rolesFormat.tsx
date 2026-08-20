import type { ReactNode } from 'react';
import { ApiError } from '../../api/client';
import type { ModuleLevel, Role } from '../../api/roles';

/** Shared formatting + label helpers for the Roles admin screens. */

/** Pull a human string out of any thrown value — never "[object Object]". */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  return 'Something went wrong.';
}

/** A module-level selection in the editor: '' means None (module omitted). */
export type LevelValue = '' | ModuleLevel;

/** The segmented-control options, in ascending-privilege order. */
export const LEVEL_OPTIONS: { value: LevelValue; label: string }[] = [
  { value: '', label: 'None' },
  { value: 'VIEW', label: 'View' },
  { value: 'OPERATE', label: 'Operate' },
  { value: 'MANAGE', label: 'Manage' },
];

/** Human labels for a granted level. */
export const LEVEL_LABEL: Record<ModuleLevel, string> = {
  VIEW: 'View',
  OPERATE: 'Operate',
  MANAGE: 'Manage',
};

/** The platform-wide permissions a role may toggle on. */
export interface PlatformPermission {
  key: string;
  label: string;
  hint: string;
}
export const PLATFORM_PERMISSIONS: PlatformPermission[] = [
  { key: 'iam', label: 'Manage users & roles', hint: 'Invite users, assign roles, edit roles.' },
  { key: 'settings', label: 'Edit settings', hint: 'Change platform-wide settings.' },
];

/** key -> label for any platform permission (falls back to the raw key). */
export const PLATFORM_LABEL: Record<string, string> = Object.fromEntries(
  PLATFORM_PERMISSIONS.map((p) => [p.key, p.label]),
);

/** A single grant chip (module+level, or a platform permission). */
function Chip({ tone = 'slate', children }: { tone?: 'slate' | 'brand'; children: ReactNode }) {
  const cls =
    tone === 'brand'
      ? 'bg-brand-50 text-brand-700'
      : 'bg-slate-100 text-slate-600';
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${cls}`}>
      {children}
    </span>
  );
}

/**
 * Compact, human-readable summary of what a role grants: one chip per module
 * (with its level) plus one chip per platform permission. Used both in the
 * roles table and — driven off the live editor selection — in the editor's
 * "what this role grants" panel.
 */
export function GrantSummary({
  moduleLevels,
  platform,
  titleByKey,
  emptyLabel = 'No access',
}: {
  moduleLevels: Record<string, ModuleLevel>;
  platform: string[];
  titleByKey: Map<string, string>;
  emptyLabel?: string;
}) {
  const modules = Object.entries(moduleLevels);
  const perms = platform.filter((p) => PLATFORM_LABEL[p] !== undefined || p.length > 0);
  if (modules.length === 0 && perms.length === 0) {
    return <span className="text-xs text-slate-400">{emptyLabel}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {modules.map(([key, level]) => (
        <Chip key={`m-${key}`}>
          {titleByKey.get(key) ?? key}: {LEVEL_LABEL[level]}
        </Chip>
      ))}
      {perms.map((p) => (
        <Chip key={`p-${p}`} tone="brand">
          {PLATFORM_LABEL[p] ?? p}
        </Chip>
      ))}
    </div>
  );
}

/** Build a title lookup from the assignable-modules list. */
export function buildTitleByKey(modules: { key: string; title: string }[]): Map<string, string> {
  const map = new Map<string, string>();
  for (const m of modules) map.set(m.key, m.title);
  return map;
}

/** Convenience: a role's granted-modules summary for the table row. The built-in
 * Administrator carries no explicit rows (it's all-access by virtue of being the system
 * role), so we say so rather than render the empty-state "No access". */
export function RoleGrantCell({
  role,
  titleByKey,
}: {
  role: Role;
  titleByKey: Map<string, string>;
}) {
  if (role.is_system) {
    return (
      <span className="text-xs font-medium text-slate-600">
        Full access — every module (Manage) + all platform permissions
      </span>
    );
  }
  return (
    <GrantSummary
      moduleLevels={role.module_levels}
      platform={role.platform}
      titleByKey={titleByKey}
    />
  );
}
