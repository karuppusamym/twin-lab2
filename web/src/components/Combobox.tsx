import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";

export interface ComboOption { value: string; label: string; hint?: string }

/**
 * A searchable list for a long choice (an application among a dozen or more): type to narrow it,
 * arrow keys to move, Enter to choose, Escape to close. ARIA 1.2 combobox with a listbox popup.
 * The form value is `value` ("" until something is chosen); `required` makes the browser refuse
 * to submit without a choice.
 */
export function Combobox({ label, options, value, onChange, placeholder = "Type to search", required, id }: {
  label: string;
  options: ComboOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
  id?: string;
}) {
  const auto = useId();
  const inputId = id ?? `combo-${auto}`;
  const listId = `${inputId}-list`;
  const chosen = options.find((o) => o.value === value);
  const [text, setText] = useState(chosen?.label ?? "");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  // the text follows the choice when it changes from outside (a reset, a default)
  useEffect(() => { setText(chosen?.label ?? ""); }, [chosen?.label]);
  const typed = text.trim().toLowerCase();
  const shown = useMemo(() => (!typed || typed === chosen?.label.toLowerCase()
    ? options : options.filter((o) => `${o.label} ${o.hint ?? ""}`.toLowerCase().includes(typed))), [options, typed, chosen?.label]);
  useEffect(() => {
    input.current?.setCustomValidity(required && !value ? `Choose one of the ${label.toLowerCase()} options.` : "");
  }, [required, value, label]);
  const pick = (o: ComboOption) => { onChange(o.value); setText(o.label); setOpen(false); };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((i) => Math.min(i + 1, shown.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter" && open && shown[active]) { e.preventDefault(); pick(shown[active]); }
    else if (e.key === "Escape") { setOpen(false); setText(chosen?.label ?? ""); }
  };
  return (
    <div className="combobox">
      <label htmlFor={inputId}>{label}</label>
      <input
        ref={input} id={inputId} role="combobox" aria-expanded={open} aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={open && shown[active] ? `${listId}-${active}` : undefined}
        autoComplete="off" value={text} placeholder={placeholder} required={required}
        onChange={(e) => { setText(e.target.value); setOpen(true); setActive(0); if (!e.target.value) onChange(""); }}
        onFocus={() => setOpen(true)} onClick={() => setOpen(true)}
        onBlur={() => { setOpen(false); setText(chosen?.label ?? ""); }}
        onKeyDown={onKey}
      />
      <ul id={listId} role="listbox" aria-label={label} hidden={!open}>
        {shown.map((o, i) => (
          <li key={o.value} id={`${listId}-${i}`} role="option" aria-selected={o.value === value}
            className={i === active ? "active" : undefined}
            onMouseDown={(e) => { e.preventDefault(); pick(o); }} onMouseEnter={() => setActive(i)}>
            {o.label}{o.hint && <span className="muted"> {o.hint}</span>}
          </li>
        ))}
        {shown.length === 0 && <li role="presentation" className="muted">No match for “{text.trim()}”</li>}
      </ul>
      <span className="visually-hidden" role="status">{open ? `${shown.length} of ${options.length} options` : ""}</span>
    </div>
  );
}
