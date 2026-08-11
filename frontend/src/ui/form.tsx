import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react';

/** Form controls with a consistent label/error scaffold. */

function Field({
  label,
  required,
  error,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 flex items-center gap-1 text-xs font-medium text-slate-600">
        {label}
        {required && <span className="text-rose-500">*</span>}
      </span>
      {children}
      {error ? (
        <span className="mt-1 block text-xs text-rose-600">{error}</span>
      ) : (
        hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>
      )}
    </label>
  );
}

const CONTROL =
  'w-full rounded-md border bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-brand-500/40';
const ok = 'border-slate-300 focus:border-brand-500';
const bad = 'border-rose-400 focus:border-rose-500';

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  label: string;
  error?: string;
  hint?: string;
}

export function TextField({ label, error, hint, required, className = '', ...rest }: TextFieldProps) {
  return (
    <Field label={label} required={required} error={error} hint={hint}>
      <input {...rest} required={required} className={`${CONTROL} ${error ? bad : ok} ${className}`} />
    </Field>
  );
}

export interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label: string;
  error?: string;
  hint?: string;
}

export function TextArea({ label, error, hint, required, className = '', ...rest }: TextAreaProps) {
  return (
    <Field label={label} required={required} error={error} hint={hint}>
      <textarea {...rest} required={required} className={`${CONTROL} ${error ? bad : ok} ${className}`} />
    </Field>
  );
}

export interface SelectFieldProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}

export function SelectField({ label, error, hint, required, className = '', children, ...rest }: SelectFieldProps) {
  return (
    <Field label={label} required={required} error={error} hint={hint}>
      <select {...rest} required={required} className={`${CONTROL} ${error ? bad : ok} ${className}`}>
        {children}
      </select>
    </Field>
  );
}
