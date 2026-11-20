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

// Upload progress: refreshed from the storage API while the upload is not complete.
const upload = document.querySelector("[data-upload-url]");
if (upload && upload.dataset.complete !== "1") {
  const refresh = async () => {
    try {
      const response = await fetch(upload.dataset.uploadUrl, {headers: {Accept: "application/json"}});
      if (!response.ok) return;
      const data = await response.json();
      const state = String(data.state || data.status || "uploading");
      const done = data.received_bytes ?? data.bytes_received ?? data.uploaded_bytes;
      const total = data.total_bytes ?? data.bytes_total ?? data.size;
      let percent = data.percent ?? (done != null && total ? Math.round(done / total * 100) : null);
      upload.textContent = state.charAt(0).toUpperCase() + state.slice(1).replace(/_/g, " ")
        + (percent != null ? ` ${Math.max(0, Math.min(100, Math.round(percent)))}%` : "")
        + (data.last_error ? ` · ${String(data.last_error).slice(0, 300)}` : "");
      // confirmed: stored and checked; rejected: the recorder starts a new upload
      if (/^(complete|completed|done|uploaded|stored|confirmed|rejected)$/i.test(state) || percent >= 100) return;
    } catch (error) {
      return;
    }
    setTimeout(refresh, 5000);
  };
  setTimeout(refresh, 5000);
}
