for (const button of document.querySelectorAll("[data-select-source]")) {
  const events = [...button.closest("fieldset").querySelectorAll("input[type=checkbox]")];
  const updateLabel = () => {
    button.textContent = events.every(event => event.checked) ? "Clear all" : "Select all";
  };
  button.hidden = false;
  button.addEventListener("click", () => {
    const select = events.some(event => !event.checked);
    for (const event of events) event.checked = select;
    updateLabel();
  });
  for (const event of events) event.addEventListener("change", updateLabel);
  updateLabel();
}
