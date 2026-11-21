// Narrow screens show a data table one row per card: each cell says which column it is. The
// stylesheet does the layout (table.stack under 600px); this gives each body cell its column's
// header text as `data-label`, so every table that has a header row stacks without each page
// having to repeat its column names.

/** The header text of each column of `table`'s last header row, spread over colSpan. */
function columnNames(table: HTMLTableElement): string[] | null {
  const head = table.tHead?.rows[table.tHead.rows.length - 1];
  if (!head) return null;
  const names: string[] = [];
  for (const cell of Array.from(head.cells)) {
    const text = (cell.textContent ?? "").trim();
    for (let i = 0; i < cell.colSpan; i++) names.push(text);
  }
  return names;
}

/** Labels the body cells of one table and marks it as one that may stack. */
export function labelTable(table: HTMLTableElement) {
  const names = columnNames(table);
  if (!names) return;
  for (const body of Array.from(table.tBodies)) {
    for (const row of Array.from(body.rows)) {
      let column = 0;
      for (const cell of Array.from(row.cells)) {
        const name = names[column];
        // a row header is the card's title; a cell spanning every column is a message row
        const titled = cell.tagName === "TH" || cell.colSpan >= names.length;
        if (name && !titled) {
          if (cell.dataset.label !== name) cell.dataset.label = name;
        } else if (cell.dataset.label !== undefined) {
          delete cell.dataset.label;
        }
        column += cell.colSpan;
      }
    }
  }
  if (!table.classList.contains("stack")) table.classList.add("stack");
}

/** Labels every `table.flow` in `root` now and whenever its content changes; returns the stop function. */
export function stackTables(root: HTMLElement): () => void {
  const run = () => root.querySelectorAll<HTMLTableElement>("table.flow").forEach(labelTable);
  run();
  if (typeof MutationObserver === "undefined") return () => undefined;
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      run();
    });
  });
  // attributes are not watched: labelTable's own writes would wake the observer again
  observer.observe(root, { childList: true, subtree: true, characterData: true });
  return () => observer.disconnect();
}
