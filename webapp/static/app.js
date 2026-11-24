const scan = document.querySelector("#scan");

if (scan) {
  const form = document.querySelector("#batch-form");
  const zone = document.querySelector("#drop-zone");
  const status = document.querySelector("#upload-status");
  const remove = document.querySelector("#remove-file");
  const submit = document.querySelector("#submit-batch");
  const csrf = form.elements.csrf.value;
  let check;

  function describe(message, failed = false) {
    status.hidden = !message;
    status.textContent = message;
    status.classList.toggle("warning", failed);
  }

  async function inspect() {
    check?.abort();
    check = undefined;
    const file = scan.files[0];
    submit.disabled = false;
    remove.hidden = !file;
    scan.setCustomValidity("");
    if (!file) {
      describe("");
      return;
    }
    const volume = file.size >= 1048576
      ? (file.size / 1048576).toFixed(1) + " MiB"
      : (file.size / 1024).toFixed(1) + " KiB";
    const name = file.name + " · " + volume;
    if (!file.name.toLowerCase().endsWith(".pdf") || file.size > Number(scan.dataset.max)) {
      const error = "Choose a PDF within the upload limit.";
      scan.setCustomValidity(error);
      describe(name + " — " + error, true);
      return;
    }

    check = new AbortController();
    const current = check;
    describe(name + " · Checking page count…");
    submit.disabled = true;
    const data = new FormData();
    data.append("scan", file);
    try {
      const response = await fetch(scan.dataset.inspect, {
        method: "POST",
        headers: { "X-CSRF-Token": csrf },
        body: data,
        signal: current.signal,
        cache: "no-store",
      });
      const validated = await response.json();
      if (current !== check) return;
      if (!response.ok) {
        const error = validated.detail || "The PDF could not be validated.";
        if (response.status === 413 || response.status === 422) scan.setCustomValidity(error);
        describe(name + " — " + error, true);
        return;
      }
      describe(name + " · " + validated.pages + (validated.pages === 1 ? " page" : " pages"));
    } catch (error) {
      if (error.name !== "AbortError" && current === check) {
        describe(name + " — Page count is unavailable. Validation will run on submission.", true);
      }
    } finally {
      if (current === check) submit.disabled = false;
    }
  }

  scan.addEventListener("change", inspect);
  remove.addEventListener("click", () => {
    scan.value = "";
    inspect();
    scan.focus();
  });

  for (const transition of ["dragover", "dragleave", "drop"]) {
    zone.addEventListener(transition, event => {
      event.preventDefault();
      zone.classList.toggle("dragging", transition === "dragover");
    });
  }
  zone.addEventListener("drop", event => {
    if (event.dataTransfer.files.length !== 1) {
      describe("Drop one scanned PDF at a time.", true);
      return;
    }
    scan.files = event.dataTransfer.files;
    inspect();
  });

  const ciloCount = document.querySelector("#cilo-count");
  function updateCilos() {
    for (const label of document.querySelectorAll("[data-cilo]")) {
      label.hidden = Number(label.dataset.cilo) > Number(ciloCount.value);
      label.querySelector("textarea").disabled = label.hidden;
    }
  }
  updateCilos();
  ciloCount.addEventListener("change", updateCilos);
  form.addEventListener("submit", () => {
    submit.disabled = true;
    submit.textContent = "Uploading…";
  });
}

const progress = document.querySelector("#job-progress");
if (progress) {
  const state = document.querySelector("#job-state");
  async function poll() {
    try {
      const response = await fetch(progress.dataset.url, { cache: "no-store" });
      if (!response.ok) throw new Error("Status unavailable");
      const job = await response.json();
      if (!["Queued", "Processing"].includes(job.status)) {
        window.location.reload();
        return;
      }
      state.textContent = job.status;
      const elapsed = job.status === "Processing" ? " · " + job.elapsed_seconds + " s elapsed" : "";
      const count = job.processed === null ? job.total + " pages" :
        job.processed + " of " + job.total + " pages recorded · " +
        job.successful + " recognized · " + job.failed + " failed";
      progress.textContent = count + " · " + job.status + elapsed +
        ". You can close this page while the worker runs.";
    } catch {
      progress.textContent = "Could not refresh status. Reconnecting…";
    }
    window.setTimeout(poll, 2000);
  }
  window.setTimeout(poll, 1000);
}
