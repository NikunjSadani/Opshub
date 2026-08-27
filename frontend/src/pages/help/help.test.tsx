import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { HelpModule } from './HelpModule';

// HelpModule is static content (no query/auth), so a MemoryRouter is the only wrapper needed.
function renderHelp() {
  return render(
    <MemoryRouter>
      <HelpModule />
    </MemoryRouter>,
  );
}

describe('HelpModule', () => {
  it('renders the page and the delivery-challan guide', () => {
    renderHelp();
    expect(screen.getByText('Help & Guides')).toBeInTheDocument();
    expect(screen.getByText('How to create a Delivery Challan')).toBeInTheDocument();
  });

  it('documents the real prerequisite + generate steps', () => {
    renderHelp();
    // Prerequisite chain the owner explicitly asked to include (exact step titles).
    expect(screen.getByText('Create an active project')).toBeInTheDocument();
    expect(screen.getByText('Load master data (consignor + HSN codes)')).toBeInTheDocument();
    // The actual challan flow is a template upload, not a per-record form.
    expect(screen.getByText('Download and fill the template')).toBeInTheDocument();
    expect(screen.getByText('Number & generate')).toBeInTheDocument();
  });
});
