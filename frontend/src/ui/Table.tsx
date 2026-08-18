import type { ReactNode } from 'react';

/** Thin, consistent table shell. Wrap rows in <Table><THead/><tbody/></Table>. */

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export function THead({ children }: { children: ReactNode }) {
  return (
    <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
      {children}
    </thead>
  );
}

export function Th({ className = '', children }: { className?: string; children?: ReactNode }) {
  return <th scope="col" className={`px-3 py-2 font-semibold ${className}`}>{children}</th>;
}

export function Tr({ className = '', children }: { className?: string; children: ReactNode }) {
  return <tr className={`border-b border-slate-100 last:border-0 ${className}`}>{children}</tr>;
}

export function Td({ className = '', children }: { className?: string; children?: ReactNode }) {
  return <td className={`px-3 py-2 align-middle text-slate-700 ${className}`}>{children}</td>;
}
