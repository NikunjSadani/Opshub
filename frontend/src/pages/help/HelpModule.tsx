import { type ReactNode } from 'react';
import { Card, PageHeader } from '../../ui';

/**
 * Help & Guides — in-app operator documentation. Visible to every signed-in staff
 * member (no permission gate beyond auth), reached from the sidebar "Help & Guides"
 * link and the /help route.
 *
 * Guides are hand-authored JSX (no markdown dependency) using the app's own
 * primitives, so they inherit the product look and stay type-checked. The content
 * mirrors the REAL screens/labels of the Delivery Challan flow — it is not a generic
 * description. Add future guides as new <Guide> sections below.
 */

function Guide({ title, intro, children }: { title: string; intro: ReactNode; children: ReactNode }) {
  return (
    <Card className="p-6 sm:p-8">
      <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
      <p className="mt-1 max-w-2xl text-sm text-slate-600">{intro}</p>
      <div className="mt-6 space-y-8">{children}</div>
    </Card>
  );
}

function Section({ heading, children }: { heading: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-500">{heading}</h3>
      <ol className="mt-3 space-y-4">{children}</ol>
    </section>
  );
}

/** One numbered step. `n` is shown in a brand chip; `title` is the action, `children` the detail. */
function Step({ n, title, children }: { n: number; title: string; children?: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand-50 text-xs font-semibold text-brand-700">
        {n}
      </span>
      <div className="min-w-0">
        <p className="text-sm font-medium text-slate-900">{title}</p>
        {children && <div className="mt-1 space-y-1 text-sm text-slate-600">{children}</div>}
      </div>
    </li>
  );
}

/** A path a user follows through the UI, e.g. Projects › Clients › New client. */
function Path({ children }: { children: ReactNode }) {
  return (
    <span className="whitespace-nowrap rounded bg-slate-100 px-1.5 py-0.5 text-[13px] font-medium text-slate-700">
      {children}
    </span>
  );
}

function Callout({ tone, children }: { tone: 'tip' | 'warn'; children: ReactNode }) {
  const styles =
    tone === 'warn'
      ? 'border-amber-300 bg-amber-50 text-amber-900'
      : 'border-brand-200 bg-brand-50 text-brand-900';
  const label = tone === 'warn' ? 'Important' : 'Tip';
  return (
    <div className={`rounded-lg border px-3 py-2 text-sm ${styles}`}>
      <span className="font-semibold">{label}: </span>
      {children}
    </div>
  );
}

function DeliveryChallanGuide() {
  return (
    <Guide
      title="How to create a Delivery Challan"
      intro={
        <>
          Delivery challans are produced <strong>in bulk from an Excel template</strong> that you fill
          in and upload — there is no one-challan-at-a-time form. Before your first batch you set up a
          few things once (a client, a project, and master data such as HSN codes). After that, each run
          is: download the template → fill it → upload &amp; validate → number &amp; generate → download the
          PDFs.
        </>
      }
    >
      <Section heading="Before you start — one-time setup">
        <Step n={1} title="Create the client (the party you ship for)">
          <p>
            Go to <Path>Projects › Clients</Path> and click <strong>New client</strong>. Note the
            client <strong>Code</strong> — project codes are built from it.
          </p>
          <p>
            For challans that will carry a scannable QR, also set{' '}
            <strong>Access PIN (challan QR)</strong> on the client — it forms part of the QR password.
          </p>
          <p className="text-slate-400">Requires Manage access on Projects.</p>
        </Step>
        <Step n={2} title="Create an active project">
          <p>
            Go to <Path>Projects › Projects</Path> and click <strong>New project</strong>. Pick the
            client and give it a name.
          </p>
          <p>
            The <strong>Project ID</strong> is generated automatically as{' '}
            <code className="rounded bg-slate-100 px-1 text-[13px]">CLIENTCODE-number</code> (e.g.{' '}
            <code className="rounded bg-slate-100 px-1 text-[13px]">ACME-1</code>). You will type this
            exact code into the spreadsheet later, and the project must be <strong>Active</strong>.
          </p>
          <p className="text-slate-400">Requires Operate access on Projects.</p>
        </Step>
        <Step n={3} title="Load master data (consignor + HSN codes)">
          <p>
            Go to <Path>Delivery Challan › Master Data</Path>. Add your <strong>Consignor</strong> (the
            dispatching entity that appears on every challan) and the <strong>HSN Codes</strong> you
            ship under.
          </p>
          <Callout tone="warn">
            HSN codes are mandatory. A spreadsheet row whose HSN is not present (and active) here is
            rejected during validation.
          </Callout>
          <p className="text-slate-400">Requires Manage access on Delivery Challan.</p>
        </Step>
        <Step n={4} title="(Optional) Purchase order & client invoice">
          <p>
            Only needed if you want the challan's QR code to open the client's invoice. Record the PO
            under <Path>Purchase Orders</Path> and upload + <strong>confirm</strong> the invoice under{' '}
            <Path>Billing &amp; AR</Path>. On the challan spreadsheet the <strong>PO Number</strong> and{' '}
            <strong>Invoice Number</strong> are optional free-text columns.
          </p>
        </Step>
      </Section>

      <Section heading="Creating the delivery challans">
        <Step n={1} title="Open New Challan">
          <p>
            Go to <Path>Delivery Challan › New Challan</Path>.
          </p>
        </Step>
        <Step n={2} title="Download and fill the template">
          <p>
            Click <strong>Download template</strong>. Fill the <strong>Challans</strong> sheet; the{' '}
            <strong>Instructions</strong> sheet explains every column.
          </p>
          <p>
            <strong>Required columns:</strong> Challan Group, Project ID, Ship-to Name, Ship-to Address
            Line 1, Ship-to State, Consignee Name, Consignee GSTIN, Challan Date, Description, HSN,
            Quantity.
          </p>
          <p>
            <strong>Optional:</strong> PO Number, Invoice Number, Rate, Amount, GST Rate, and the extra
            ship-to / consignee address fields.
          </p>
          <Callout tone="tip">
            To put several line items on one challan, give those rows the same{' '}
            <strong>Challan Group</strong>. Every challan-level field (date, ship-to, consignee…) must
            match across rows in the same group.
          </Callout>
        </Step>
        <Step n={3} title="Upload & validate">
          <p>
            Choose your filled <code className="rounded bg-slate-100 px-1 text-[13px]">.xlsx</code> and
            click <strong>Upload &amp; validate</strong>. Nothing is numbered yet — this is a safe check.
          </p>
          <p>
            If it fails, click <strong>Download error report</strong> to see the exact Row / Column /
            Problem, fix the sheet, and re-upload. If a consignee's GSTIN differs from the saved record,
            the batch needs review — resolve each flagged row before generating.
          </p>
          <p className="text-slate-400">Requires Operate access on Delivery Challan.</p>
        </Step>
        <Step n={4} title="Number & generate">
          <p>
            Once the batch is validated, set the <strong>Series</strong> (a short prefix, default{' '}
            <code className="rounded bg-slate-100 px-1 text-[13px]">L</code>) and click{' '}
            <strong>Generate challan(s)</strong>.
          </p>
          <Callout tone="warn">
            Challan numbers are assigned now (for example{' '}
            <code className="rounded bg-slate-100 px-1 text-[13px]">GIF/DC/26-27/L/000189</code>) and
            cannot be reused — so generate only when the sheet is final. Validating is free; a number is
            only consumed at Generate.
          </Callout>
        </Step>
        <Step n={5} title="Download the challans">
          <p>
            When it completes, click <strong>Download ZIP</strong> (individual PDFs) or{' '}
            <strong>Download merged PDF</strong>. You can also re-download later from the{' '}
            <strong>Batches</strong> and <strong>Download</strong> tabs.
          </p>
          <Callout tone="tip">
            Each challan prints as a half-A4. The merged PDF places <strong>two challans per A4</strong>{' '}
            sheet at full size (no shrinking) to save paper.
          </Callout>
        </Step>
      </Section>

      <Section heading="Good to know">
        <Step n={1} title="The Project ID must match exactly">
          <p>
            The <strong>Project ID</strong> in the sheet must be the exact auto-generated project code,
            and the project must be <strong>Active</strong> — otherwise every row referencing it fails
            validation.
          </p>
        </Step>
        <Step n={2} title="The QR is off unless enabled for your setup">
          <p>
            When switched on, the QR password is the client's <strong>Access PIN</strong> followed by
            the <strong>challan number</strong>, and it opens the invoice only once that invoice has
            been uploaded and <strong>confirmed</strong> under Billing &amp; AR.
          </p>
        </Step>
      </Section>

      <Section heading="Who can do what">
        <li>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[420px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-slate-500">
                  <th className="py-2 pr-4 font-medium">Action</th>
                  <th className="py-2 font-medium">Access needed</th>
                </tr>
              </thead>
              <tbody className="text-slate-700">
                <tr className="border-b border-slate-100">
                  <td className="py-2 pr-4">View challans, register, downloads</td>
                  <td className="py-2">Any access to Delivery Challan</td>
                </tr>
                <tr className="border-b border-slate-100">
                  <td className="py-2 pr-4">Upload, validate, generate</td>
                  <td className="py-2">Operate on Delivery Challan</td>
                </tr>
                <tr className="border-b border-slate-100">
                  <td className="py-2 pr-4">Master Data, void / recover</td>
                  <td className="py-2">Manage on Delivery Challan</td>
                </tr>
                <tr className="border-b border-slate-100">
                  <td className="py-2 pr-4">Create a project</td>
                  <td className="py-2">Operate on Projects</td>
                </tr>
                <tr>
                  <td className="py-2 pr-4">Manage a client / set Access PIN</td>
                  <td className="py-2">Manage on Projects</td>
                </tr>
              </tbody>
            </table>
          </div>
        </li>
      </Section>
    </Guide>
  );
}

export function HelpModule() {
  return (
    <div className="mx-auto max-w-4xl">
      <PageHeader title="Help & Guides" subtitle="Step-by-step how-tos for OpsHub." />
      <DeliveryChallanGuide />
    </div>
  );
}
