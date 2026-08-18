import { useState } from 'react';
import { Button } from '../../ui';

/**
 * Shows a one-time password-setup link (or an honest "no link yet" note when
 * the backend returns null because auth isn't configured). The link is shown
 * ONCE — we never claim an email was sent, because delivery isn't wired.
 */
export function SetupLinkPanel({ setupLink }: { setupLink: string | null }) {
  const [copied, setCopied] = useState(false);

  if (!setupLink) {
    return (
      <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-600">
        No setup link yet — this user isn't backed by a real auth account (auth isn't
        configured in this environment). Once Firebase auth is wired, re-issue the link
        here and share it with the user.
      </div>
    );
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(setupLink ?? '');
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked (e.g. no permission) — the field is selectable, so the
      // admin can copy manually. Don't pretend it succeeded.
      setCopied(false);
    }
  }

  return (
    <div className="space-y-2">
      <p className="text-sm font-medium text-slate-700">
        Send this password-setup link to the user
      </p>
      <div className="flex items-stretch gap-2">
        <input
          readOnly
          aria-label="Password setup link"
          value={setupLink}
          onFocus={(e) => e.target.select()}
          className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500/40"
        />
        <Button variant="secondary" size="sm" onClick={() => void copy()}>
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <p className="text-xs text-slate-400">
        Shown once — email delivery isn't wired yet, so copy it now and share it
        directly. It won't be displayed again.
      </p>
    </div>
  );
}
