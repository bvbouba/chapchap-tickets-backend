/* PDF rendering uses Mozilla PDF.js 4.10.38, Apache-2.0, vendored locally. */
var copy = window.PRINTME_KIOSK_COPY || {};
var root = document.querySelector(".kiosk");

if (root) {
  var idleMs = Number(root.getAttribute("data-idle-ms")) || 120000;
  var warningMs = Number(root.getAttribute("data-idle-warning-ms")) || 15000;
  var idleTimer = null;
  var warningTimer = null;
  var ticket = 0;
  var busy = false;
  var code = "";
  var copies = 1;
  var colorMode = "black-and-white";
  var sides = "one-sided";
  var pageSelection = "all";
  var pageDraft = "all";
  var colorDraft = "black-and-white";
  var sidesDraft = "one-sided";
  var quoteReady = false;
  var documents = [];
  var selectedId = "";
  var pdfEngine = null;
  var pdfLoading = null;
  var pdfDoc = null;
  var pdfPage = 1;
  var zoomMode = "fit";
  var manualScale = 1;
  var previewUrl = "";
  var fitTimer = null;
  var printGeneration = 0;
  var printTimer = null;
  var printWatching = false;

  var codeField = root.querySelector("[data-code-field]");
  var codeError = root.querySelector("[data-code-error]");
  var documentList = root.querySelector("[data-document-list]");
  var documentsNote = root.querySelector("[data-documents-note]");
  var openPreviewButton = root.querySelector("[data-open-preview]");
  var continueButton = root.querySelector("[data-continue]");
  var previewNote = root.querySelector("[data-preview-note]");
  var previewCanvas = root.querySelector("[data-preview-canvas]");
  var previewStage = root.querySelector("[data-preview-stage]");
  var pageLabel = root.querySelector("[data-page-label]");
  var fewerCopies = root.querySelector("[data-copies-delta='-1']");
  var moreCopies = root.querySelector("[data-copies-delta='1']");
  var busyOverlay = root.querySelector("[data-overlay='busy']");
  var busyMessage = root.querySelector("[data-busy-message]");
  var busyDetail = root.querySelector("[data-busy-detail]");
  var idleOverlay = root.querySelector("[data-overlay='idle']");

  function show(name) {
    var screens = root.querySelectorAll("[data-screen]");
    for (var index = 0; index < screens.length; index += 1) {
      screens[index].hidden = screens[index].getAttribute("data-screen") !== name;
    }
    if (name === "code" && codeField) {
      codeField.focus();
    }
  }

  function visibleScreen() {
    var screens = root.querySelectorAll("[data-screen]");
    for (var index = 0; index < screens.length; index += 1) {
      if (!screens[index].hidden) {
        return screens[index].getAttribute("data-screen");
      }
    }
    return "";
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function nextTicket() {
    ticket += 1;
    return ticket;
  }

  function isCurrent(id) {
    return id === ticket;
  }

  function clearIdleTimers() {
    window.clearTimeout(idleTimer);
    window.clearTimeout(warningTimer);
    idleTimer = null;
    warningTimer = null;
  }

  function hideIdleWarning() {
    idleOverlay.hidden = true;
  }

  function pauseIdle() {
    clearIdleTimers();
    hideIdleWarning();
  }

  function armIdle() {
    if (busy || printWatching) {
      return;
    }
    clearIdleTimers();
    hideIdleWarning();
    idleTimer = window.setTimeout(showIdleWarning, idleMs);
  }

  function showIdleWarning() {
    if (busy) {
      return;
    }
    idleOverlay.hidden = false;
    warningTimer = window.setTimeout(resetToWelcome, warningMs);
  }

  function beginBusy(message, detail) {
    busy = true;
    pauseIdle();
    busyMessage.textContent = message;
    busyDetail.textContent = detail || "";
    busyOverlay.hidden = false;
  }

  function setBusyMessage(message, detail) {
    busyMessage.textContent = message;
    if (detail !== undefined) {
      busyDetail.textContent = detail;
    }
  }

  function endBusy() {
    busy = false;
    busyOverlay.hidden = true;
    armIdle();
  }

  function renderCode() {
    if (codeField) {
      codeField.value = code;
    }
    continueButton.disabled = code.length !== 8 || busy;
  }

  function appendCode(raw) {
    var character = String(raw || "").toUpperCase();
    if (!/^[0-9A-F]$/.test(character) || code.length >= 8) {
      return;
    }
    code += character;
    codeError.textContent = "";
    renderCode();
  }

  function renderCopies() {
    var counts = root.querySelectorAll("[data-copy-count]");
    for (var index = 0; index < counts.length; index += 1) {
      counts[index].textContent = String(copies);
    }
    fewerCopies.disabled = copies <= 1;
    moreCopies.disabled = copies >= 99;
  }

  function closeOptionDrawer() {
    var panels = root.querySelectorAll("[data-option-panel]");
    var rows = root.querySelectorAll(".option-row[data-option]");
    for (var panelIndex = 0; panelIndex < panels.length; panelIndex += 1) {
      panels[panelIndex].hidden = true;
    }
    for (var index = 0; index < rows.length; index += 1) {
      rows[index].setAttribute("aria-pressed", "false");
    }
  }

  function openOptionDrawer(name) {
    colorDraft = colorMode;
    sidesDraft = sides;
    pageDraft = pageSelection === "all" ? "all" : "custom";
    pressGroup("[data-color]", colorDraft, "data-color");
    pressGroup("[data-sides]", sidesDraft, "data-sides");
    pressGroup("[data-pages]", pageDraft, "data-pages");
    syncRangeEntry();
    var pageError = root.querySelector("[data-page-error]");
    if (pageError) {
      pageError.hidden = true;
      pageError.textContent = "";
    }
    var panels = root.querySelectorAll("[data-option-panel]");
    var rows = root.querySelectorAll(".option-row[data-option]");
    for (var index = 0; index < rows.length; index += 1) {
      var selected = rows[index].getAttribute("data-option") === name;
      rows[index].setAttribute("aria-pressed", selected ? "true" : "false");
    }
    for (var panelIndex = 0; panelIndex < panels.length; panelIndex += 1) {
      panels[panelIndex].hidden =
        panels[panelIndex].getAttribute("data-option-panel") !== name;
    }
  }

  function syncRangeEntry() {
    var rangeEntry = root.querySelector("[data-range-entry]");
    var rangeInput = root.querySelector("[data-page-range]");
    if (rangeEntry) {
      rangeEntry.hidden = pageDraft !== "custom";
    }
    if (rangeInput) {
      rangeInput.value = pageSelection === "all" ? "" : pageSelection;
    }
  }

  function pressGroup(selector, value, attribute) {
    var buttons = root.querySelectorAll(selector);
    for (var index = 0; index < buttons.length; index += 1) {
      var button = buttons[index];
      var selected = button.getAttribute(attribute) === value;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
    }
  }

  function colorLabel(value) {
    return value === "color" ? "Color" : "Black & White";
  }

  function sidesLabel(value) {
    return value === "two-sided" ? "Two-sided" : "One-sided";
  }

  function renderOptionLabels() {
    var valueLabel = root.querySelector("[data-value-label]");
    var colorButton = root.querySelector("[data-color-label]");
    var sidesButton = root.querySelector("[data-sides-label]");
    if (valueLabel) {
      valueLabel.textContent = pageSelection === "all" ? "All pages" : pageSelection;
    }
    if (colorButton) {
      colorButton.textContent = colorLabel(colorMode);
    }
    if (sidesButton) {
      sidesButton.textContent = sidesLabel(sides);
    }
  }

  function showPageError(message) {
    var pageError = root.querySelector("[data-page-error]");
    if (!pageError) {
      return;
    }
    pageError.hidden = false;
    pageError.textContent = message;
  }

  function commitPages() {
    if (pageDraft !== "custom") {
      pageSelection = "all";
      var pageError = root.querySelector("[data-page-error]");
      if (pageError) {
        pageError.hidden = true;
        pageError.textContent = "";
      }
      return true;
    }
    var rangeInput = root.querySelector("[data-page-range]");
    var cleaned = String(rangeInput ? rangeInput.value : "").replace(/\s+/g, "");
    if (!cleaned) {
      showPageError("Enter a page range.");
      return false;
    }
    if (cleaned.length > 128 || !/^(\d+-\d+|\d+)(,(\d+-\d+|\d+))*$/.test(cleaned)) {
      showPageError("That page range is not valid.");
      return false;
    }
    var parts = cleaned.split(",");
    for (var index = 0; index < parts.length; index += 1) {
      var bits = parts[index].split("-");
      var start = Number(bits[0]);
      var end = bits.length === 2 ? Number(bits[1]) : start;
      if (start < 1) {
        showPageError("That page range is not valid.");
        return false;
      }
      if (end < start) {
        showPageError("That page range is out of order.");
        return false;
      }
    }
    pageSelection = cleaned;
    var pageError = root.querySelector("[data-page-error]");
    if (pageError) {
      pageError.hidden = true;
      pageError.textContent = "";
    }
    return true;
  }

  function selectedDocument() {
    for (var index = 0; index < documents.length; index += 1) {
      if (documents[index].id === selectedId) {
        return documents[index];
      }
    }
    return null;
  }

  function revokePreview() {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = "";
    }
  }

  function releasePreview() {
    revokePreview();
    if (pdfDoc && pdfDoc.destroy) {
      pdfDoc.destroy();
    }
    pdfDoc = null;
    pdfPage = 1;
    zoomMode = "fit";
    manualScale = 1;
    if (previewCanvas) {
      previewCanvas.hidden = true;
      var context = previewCanvas.getContext("2d");
      context.clearRect(0, 0, previewCanvas.width, previewCanvas.height);
    }
    if (pageLabel) {
      pageLabel.textContent = "";
    }
  }

  function clearPage() {
    stopPrintWatch();
    code = "";
    copies = 1;
    colorMode = "black-and-white";
    sides = "one-sided";
    pageSelection = "all";
    pageDraft = "all";
    colorDraft = colorMode;
    sidesDraft = sides;
    quoteReady = false;
    documents = [];
    selectedId = "";
    renderCode();
    renderCopies();
    pressGroup("[data-color]", colorMode, "data-color");
    pressGroup("[data-sides]", sides, "data-sides");
    pressGroup("[data-pages]", "all", "data-pages");
    renderOptionLabels();
    closeOptionDrawer();
    codeError.textContent = "";
    documentsNote.hidden = true;
    documentsNote.textContent = "";
    documentList.replaceChildren();
    openPreviewButton.hidden = true;
    previewNote.hidden = true;
    previewNote.textContent = "";
    releasePreview();
    resetPrintCopy();
  }

  function clearServerSession() {
    var token = csrfToken();
    if (!token) {
      return;
    }
    fetch(root.getAttribute("data-clear-url"), {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: { "X-CSRFToken": token },
    }).catch(function () {
      return null;
    });
  }

  function resetToWelcome() {
    nextTicket();
    busy = false;
    busyOverlay.hidden = true;
    pauseIdle();
    clearPage();
    clearServerSession();
    show("welcome");
    armIdle();
  }

  function documentKind(item) {
    var name = String(item.filename || "").toLowerCase();
    var format = String(item.format || "").toLowerCase();
    if (
      name.endsWith(".doc") ||
      name.endsWith(".docx") ||
      format === "doc" ||
      format === "docx"
    ) {
      return "word";
    }
    return "pdf";
  }

  function typeIcon(kind) {
    var icon = document.createElement("span");
    icon.className = "doc-icon doc-icon-" + kind;
    icon.setAttribute("aria-label", kind === "word" ? "Word" : "PDF");
    icon.innerHTML =
      kind === "word"
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3.5h7.2L19 8.2V20.5H7z"></path><path d="M14 3.5V8h5"></path><path d="M10 12h5M10 15h5M10 18h3.5"></path></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3.5h7.2L19 8.2V20.5H7z"></path><path d="M14 3.5V8h5"></path></svg>';
    return icon;
  }

  function renderDocuments() {
    documentList.replaceChildren();
    var table = documentList.closest("table");
    var empty = documents.length === 0;
    if (table) {
      table.hidden = empty;
    }
    documentsNote.hidden = !empty;
    documentsNote.textContent = empty
      ? copy.emptyDocumentsMessage || "No documents are available for this code."
      : "";
    var ordered = documents.slice().sort(function (left, right) {
      return String(left.filename || "").localeCompare(String(right.filename || ""), undefined, {
        sensitivity: "base",
        numeric: true,
      });
    });
    for (var index = 0; index < ordered.length; index += 1) {
      var item = ordered[index];
      var kind = documentKind(item);
      var selected = item.id === selectedId;
      var row = document.createElement("tr");
      var typeCell = document.createElement("td");
      var nameCell = document.createElement("td");
      var typeButton = document.createElement("button");
      var nameButton = document.createElement("button");
      if (selected) {
        row.className = "is-selected";
      }
      typeButton.type = "button";
      typeButton.className = "document-hit";
      typeButton.setAttribute("data-document-id", item.id);
      typeButton.setAttribute("aria-pressed", selected ? "true" : "false");
      typeButton.setAttribute("aria-label", (kind === "word" ? "Word" : "PDF") + ", " + item.filename);
      typeButton.appendChild(typeIcon(kind));
      nameButton.type = "button";
      nameButton.className = "document-hit document-name";
      nameButton.setAttribute("data-document-id", item.id);
      nameButton.setAttribute("aria-pressed", selected ? "true" : "false");
      nameButton.textContent = item.filename;
      typeCell.appendChild(typeButton);
      nameCell.appendChild(nameButton);
      row.appendChild(typeCell);
      row.appendChild(nameCell);
      documentList.appendChild(row);
    }
    openPreviewButton.hidden = !selectedId;
  }

  function selectDocument(id) {
    if (id !== selectedId) {
      releasePreview();
    }
    selectedId = id;
    renderDocuments();
  }

  function unavailablePrice() {
    return "Pricing is not yet available.";
  }

  function renderSummary() {
    renderOptionLabels();
    quoteReady = false;
    setPrintButtonDisabled(true);
    var item = root.querySelector("[data-summary-item]");
    var price = root.querySelector("[data-summary-price]");
    var count = root.querySelector("[data-summary-copies]");
    var line = root.querySelector("[data-summary-line]");
    var total = root.querySelector("[data-job-total]");
    var note = root.querySelector("[data-summary-note]");
    if (item) {
      item.textContent = "Document";
    }
    if (price) {
      price.textContent = unavailablePrice();
    }
    if (count) {
      count.textContent = String(copies);
    }
    if (line) {
      line.textContent = unavailablePrice();
    }
    if (total) {
      total.textContent = unavailablePrice();
    }
    fillConfirmation();
    if (note) {
      note.hidden = true;
      note.textContent = "";
    }
    var documentItem = selectedDocument();
    if (!documentItem) {
      return;
    }
    postJson(root.getAttribute("data-quote-url"), {
      document_id: documentItem.id,
      copies: copies,
      pages: pageSelection,
      color: colorMode,
      sides: sides,
    })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, payload: payload || {} };
        });
      })
      .then(function (result) {
        if (visibleScreen() !== "summary") {
          return;
        }
        if (!result.ok) {
          if (note) {
            note.hidden = false;
            note.textContent =
              result.payload.error || "That page range cannot be printed.";
          }
          setPrintButtonDisabled(true);
          return;
        }
        applyQuote(result.payload);
      })
      .catch(function () {
        if (visibleScreen() !== "summary") {
          return;
        }
        setPrintButtonDisabled(true);
      });
  }

  function fillConfirmation() {
    var pagesNode = root.querySelector("[data-summary-pages]");
    var colorNode = root.querySelector("[data-summary-color]");
    var sidesNode = root.querySelector("[data-summary-sides]");
    var copyNode = root.querySelector("[data-summary-copy-count]");
    if (pagesNode) {
      pagesNode.textContent = pageSelection === "all" ? "All pages" : pageSelection;
    }
    if (colorNode) {
      colorNode.textContent = colorLabel(colorMode);
    }
    if (sidesNode) {
      sidesNode.textContent = sidesLabel(sides);
    }
    if (copyNode) {
      copyNode.textContent = String(copies);
    }
  }

  function applyQuote(payload) {
    var item = root.querySelector("[data-summary-item]");
    var price = root.querySelector("[data-summary-price]");
    var count = root.querySelector("[data-summary-copies]");
    var line = root.querySelector("[data-summary-line]");
    var total = root.querySelector("[data-job-total]");
    if (item) {
      item.textContent = payload.reference || "Document";
    }
    if (price) {
      price.textContent = payload.price || unavailablePrice();
    }
    if (count) {
      count.textContent = String(payload.count || copies);
    }
    if (line) {
      line.textContent = payload.total || unavailablePrice();
    }
    if (total) {
      total.textContent = payload.job_total || unavailablePrice();
    }
    var pagesNode = root.querySelector("[data-summary-pages]");
    var colorNode = root.querySelector("[data-summary-color]");
    var sidesNode = root.querySelector("[data-summary-sides]");
    var copyNode = root.querySelector("[data-summary-copy-count]");
    if (pagesNode && payload.pages_label) {
      pagesNode.textContent = payload.pages_label;
    }
    if (colorNode && payload.color_label) {
      colorNode.textContent = payload.color_label;
    }
    if (sidesNode && payload.sides_label) {
      sidesNode.textContent = payload.sides_label;
    }
    var paperNode = root.querySelector("[data-summary-paper]");
    if (paperNode && payload.paper_label) {
      paperNode.textContent = payload.paper_label;
    }
    if (copyNode) {
      copyNode.textContent = String(payload.copies || copies);
    }
    quoteReady = true;
    setPrintButtonDisabled(false);
  }

  function requestSignal() {
    if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
      return AbortSignal.timeout(60000);
    }
    return undefined;
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      signal: requestSignal(),
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken(),
      },
      body: JSON.stringify(payload),
    });
  }

  function stopPrintWatch() {
    printGeneration += 1;
    window.clearTimeout(printTimer);
    printTimer = null;
    printWatching = false;
  }

  function setPrintButtonDisabled(disabled) {
    var button = root.querySelector("[data-go-printing]");
    if (button) {
      button.disabled = disabled;
    }
  }

  function resetPrintCopy() {
    var title = root.querySelector("[data-print-title]");
    var detail = root.querySelector("[data-print-detail]");
    if (title) {
      title.textContent = "Preparing your print job…";
    }
    if (detail) {
      detail.textContent = "No document has been sent to a printer.";
    }
    setPrintButtonDisabled(false);
  }

  function newIdempotencyKey() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    if (window.crypto && window.crypto.getRandomValues) {
      window.crypto.getRandomValues(bytes);
    }
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var hex = "";
    for (var index = 0; index < bytes.length; index += 1) {
      hex += (bytes[index] + 256).toString(16).slice(1);
    }
    return (
      hex.slice(0, 8) +
      "-" +
      hex.slice(8, 12) +
      "-" +
      hex.slice(12, 16) +
      "-" +
      hex.slice(16, 20) +
      "-" +
      hex.slice(20)
    );
  }

  function applyPrintPayload(payload) {
    var title = root.querySelector("[data-print-title]");
    var detail = root.querySelector("[data-print-detail]");
    var status = payload.status || "";
    if (title) {
      title.textContent = payload.title || "Printing failed";
    }
    if (detail) {
      detail.textContent =
        payload.message ||
        payload.error ||
        "Printing failed. The document was not printed.";
    }
    show("printing");
    return status;
  }

  function watchPrint(submissionId, generation) {
    postJson(root.getAttribute("data-print-url") + submissionId + "/", {})
      .then(function (response) {
        return response.json();
      })
      .then(function (payload) {
        if (generation !== printGeneration) {
          return;
        }
        var status = applyPrintPayload(payload || {});
        if (status === "accepted" || status === "submitted" || status === "printing") {
          printTimer = window.setTimeout(function () {
            watchPrint(submissionId, generation);
          }, 3000);
          return;
        }
        printWatching = false;
        setPrintButtonDisabled(false);
        armIdle();
      })
      .catch(function () {
        if (generation !== printGeneration) {
          return;
        }
        printWatching = false;
        applyPrintPayload({
          title: "Printing failed",
          status: "failed",
          message: "Printing failed. The kiosk could not reach the print service.",
        });
        setPrintButtonDisabled(false);
        armIdle();
      });
  }

  function submitPrint() {
    var documentItem = selectedDocument();
    if (!documentItem || busy || printWatching || !quoteReady) {
      return;
    }
    stopPrintWatch();
    var generation = printGeneration;
    setPrintButtonDisabled(true);
    beginBusy(copy.submittingMessage || "Submitting print job.");
    postJson(root.getAttribute("data-print-url"), {
      document_id: documentItem.id,
      copies: copies,
      pages: pageSelection,
      color: colorMode,
      sides: sides,
      idempotency_key: newIdempotencyKey(),
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (payload) {
        if (generation !== printGeneration) {
          return;
        }
        var status = applyPrintPayload(payload || {});
        var submissionId = payload && payload.submission_id;
        if (
          submissionId &&
          (status === "accepted" || status === "submitted" || status === "printing")
        ) {
          printWatching = true;
          endBusy();
          printTimer = window.setTimeout(function () {
            watchPrint(submissionId, generation);
          }, 3000);
          return;
        }
        printWatching = false;
        endBusy();
        setPrintButtonDisabled(false);
      })
      .catch(function () {
        if (generation !== printGeneration) {
          return;
        }
        printWatching = false;
        endBusy();
        applyPrintPayload({
          title: "Printing failed",
          status: "failed",
          message: "Printing failed. The kiosk could not reach the print service.",
        });
        setPrintButtonDisabled(false);
      });
  }

  function loadPdfEngine() {
    if (pdfEngine) {
      return Promise.resolve(pdfEngine);
    }
    if (!pdfLoading) {
      pdfLoading = import("./vendor/pdf.min.js").then(function (lib) {
        lib.GlobalWorkerOptions.workerSrc = new URL(
          "./vendor/pdf.worker.min.js",
          import.meta.url
        ).href;
        pdfEngine = lib;
        return lib;
      });
    }
    return pdfLoading;
  }

  function renderCurrentPage(attempt) {
    if (!pdfDoc) {
      return Promise.resolve();
    }
    var attemptCount = attempt || 0;
    if (
      (previewStage.clientWidth < 40 || previewStage.clientHeight < 40) &&
      attemptCount < 8
    ) {
      return new Promise(function (resolve) {
        window.requestAnimationFrame(function () {
          resolve(renderCurrentPage(attemptCount + 1));
        });
      });
    }
    return pdfDoc.getPage(pdfPage).then(function (page) {
      var base = page.getViewport({ scale: 1 });
      var widthScale = Math.max(previewStage.clientWidth - 24, 40) / base.width;
      var heightScale = Math.max(previewStage.clientHeight - 24, 40) / base.height;
      var fit = Math.min(widthScale, heightScale);
      var scale = zoomMode === "fit" ? fit : fit * manualScale;
      var viewport = page.getViewport({ scale: scale });
      previewCanvas.width = Math.floor(viewport.width);
      previewCanvas.height = Math.floor(viewport.height);
      previewCanvas.hidden = false;
      pageLabel.textContent = "Page " + pdfPage + " of " + pdfDoc.numPages;
      return page.render({
        canvasContext: previewCanvas.getContext("2d"),
        viewport: viewport,
      }).promise;
    });
  }

  function submitCode() {
    if (code.length !== 8) {
      codeError.textContent =
        copy.incompleteCodeMessage || "Enter all 8 characters to continue.";
      show("code");
      return;
    }
    if (busy) {
      return;
    }
    var id = nextTicket();
    continueButton.disabled = true;
    codeError.textContent = "";
    beginBusy(
      copy.checkingMessage || "Checking your release code…",
      copy.retrievingDetail || "This may take a moment."
    );
    postJson(root.getAttribute("data-session-url"), { code: code })
      .then(function (response) {
        return response.json().then(function (payload) {
          return { ok: response.ok, payload: payload };
        });
      })
      .then(function (result) {
        if (!isCurrent(id)) {
          return;
        }
        continueButton.disabled = false;
        if (!result.payload || result.payload.ok !== true) {
          documents = [];
          selectedId = "";
          endBusy();
          codeError.textContent =
            (result.payload && result.payload.error) ||
            copy.rejectedCodeMessage ||
            "That code cannot be used. Check the code and try again.";
          show("code");
          return;
        }
        setBusyMessage(
          copy.verifiedMessage || "Release code verified",
          copy.retrievingMessage || "Retrieving your documents…"
        );
        documents = Array.isArray(result.payload.documents)
          ? result.payload.documents
          : [];
        selectedId = "";
        releasePreview();
        renderDocuments();
        window.setTimeout(function () {
          if (!isCurrent(id)) {
            return;
          }
          endBusy();
          show("documents");
        }, 700);
      })
      .catch(function () {
        if (!isCurrent(id)) {
          return;
        }
        continueButton.disabled = false;
        endBusy();
        root.querySelector("[data-error-message]").textContent =
          copy.networkMessage ||
          "The kiosk could not reach the print service. Try again.";
        show("error");
      });
  }

  function loadPreview() {
    var documentItem = selectedDocument();
    if (!documentItem || busy) {
      return;
    }
    var id = nextTicket();
    openPreviewButton.disabled = true;
    releasePreview();
    previewNote.hidden = true;
    beginBusy(
      copy.downloadingMessage || "Downloading document…",
      documentItem.filename
    );
    if (documentItem.format !== "pdf") {
      endBusy();
      openPreviewButton.disabled = false;
      showPreviewFailure();
      return;
    }
    fetch(root.getAttribute("data-preview-base") + documentItem.id + "/preview/", {
      method: "POST",
      credentials: "same-origin",
      signal: requestSignal(),
      headers: { "X-CSRFToken": csrfToken() },
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("preview");
        }
        return response.arrayBuffer();
      })
      .then(function (buffer) {
        if (!isCurrent(id)) {
          return null;
        }
        setBusyMessage(
          copy.generatingMessage || "Generating document preview…",
          documentItem.filename
        );
        return loadPdfEngine().then(function (lib) {
          return lib.getDocument({ data: new Uint8Array(buffer) }).promise;
        });
      })
      .then(function (pdf) {
        if (!pdf || !isCurrent(id)) {
          if (pdf && pdf.destroy) {
            pdf.destroy();
          }
          return null;
        }
        pdfDoc = pdf;
        pdfPage = 1;
        zoomMode = "fit";
        manualScale = 1;
        openPreviewButton.disabled = false;
        endBusy();
        show("preview");
        return renderCurrentPage();
      })
      .then(function (rendered) {
        if (rendered === null || !isCurrent(id)) {
          return;
        }
      })
      .catch(function () {
        if (!isCurrent(id)) {
          return;
        }
        openPreviewButton.disabled = false;
        endBusy();
        showPreviewFailure();
      });
  }

  function showPreviewFailure() {
    releasePreview();
    previewNote.hidden = false;
    previewNote.textContent =
      copy.previewUnavailableMessage ||
      "A preview cannot be shown for this document.";
    show("preview");
  }

  function onActivity(event) {
    if (!event.isTrusted || busy) {
      return;
    }
    armIdle();
  }

  function clearPressedKeys() {
    var keys = root.querySelectorAll(".keypad button.is-pressed");
    for (var index = 0; index < keys.length; index += 1) {
      keys[index].classList.remove("is-pressed");
    }
  }

  root.addEventListener("pointerdown", onActivity);
  root.addEventListener("pointerdown", function (event) {
    var key = event.target.closest(".keypad button");
    if (key) {
      key.classList.add("is-pressed");
    }
  });
  root.addEventListener("pointerup", clearPressedKeys);
  root.addEventListener("pointercancel", clearPressedKeys);
  root.addEventListener("keydown", onActivity);
  root.addEventListener("wheel", onActivity, { passive: true });

  root.addEventListener("keydown", function (event) {
    if (visibleScreen() !== "code" || busy) {
      return;
    }
    if (event.key === "Backspace") {
      event.preventDefault();
      code = code.slice(0, -1);
      codeError.textContent = "";
      renderCode();
      return;
    }
    if (/^[0-9a-f]$/i.test(event.key)) {
      event.preventDefault();
      appendCode(event.key);
    }
  });

  root.addEventListener("click", function (event) {
    var target = event.target.closest("button");
    if (!target || !root.contains(target) || target.disabled) {
      return;
    }
    if (target.hasAttribute("data-stay")) {
      armIdle();
      return;
    }
    if (target.hasAttribute("data-start-over") || target.hasAttribute("data-go-home")) {
      resetToWelcome();
      return;
    }
    var digit = target.getAttribute("data-digit");
    if (digit) {
      appendCode(digit);
      return;
    }
    if (target.hasAttribute("data-delete")) {
      code = code.slice(0, -1);
      renderCode();
      codeError.textContent = "";
      return;
    }
    if (target.hasAttribute("data-continue")) {
      submitCode();
      return;
    }
    var documentId = target.getAttribute("data-document-id");
    if (documentId) {
      selectDocument(documentId);
      return;
    }
    if (target.hasAttribute("data-open-preview")) {
      loadPreview();
      return;
    }
    var optionName = target.getAttribute("data-option");
    if (optionName && optionName !== "copies") {
      openOptionDrawer(optionName);
      return;
    }
    if (target.hasAttribute("data-option-ok")) {
      var panel = target.closest("[data-option-panel]");
      var panelName = panel ? panel.getAttribute("data-option-panel") : "";
      if (panelName === "pages" && !commitPages()) {
        return;
      }
      if (panelName === "color") {
        colorMode = colorDraft;
      }
      if (panelName === "sides") {
        sides = sidesDraft;
      }
      renderOptionLabels();
      closeOptionDrawer();
      return;
    }
    var delta = Number(target.getAttribute("data-copies-delta"));
    if (delta === 1 || delta === -1) {
      copies = Math.min(99, Math.max(1, copies + delta));
      renderCopies();
      return;
    }
    var colorChoice = target.getAttribute("data-color");
    if (colorChoice) {
      colorDraft = colorChoice;
      pressGroup("[data-color]", colorDraft, "data-color");
      return;
    }
    var sideChoice = target.getAttribute("data-sides");
    if (sideChoice) {
      sidesDraft = sideChoice;
      pressGroup("[data-sides]", sidesDraft, "data-sides");
      return;
    }
    var pageChoice = target.getAttribute("data-pages");
    if (pageChoice) {
      pageDraft = pageChoice;
      pressGroup("[data-pages]", pageDraft, "data-pages");
      var rangeEntry = root.querySelector("[data-range-entry]");
      if (rangeEntry) {
        rangeEntry.hidden = pageDraft !== "custom";
      }
      return;
    }
    var zoom = target.getAttribute("data-zoom");
    if (zoom === "in" || zoom === "out") {
      zoomMode = "manual";
      manualScale = Math.min(4, Math.max(0.5, manualScale * (zoom === "in" ? 1.25 : 0.8)));
      renderCurrentPage();
      return;
    }
    var pageDelta = Number(target.getAttribute("data-page"));
    if ((pageDelta === 1 || pageDelta === -1) && pdfDoc) {
      var nextPage = pdfPage + pageDelta;
      if (nextPage >= 1 && nextPage <= pdfDoc.numPages) {
        pdfPage = nextPage;
        renderCurrentPage();
      }
      return;
    }
    if (target.hasAttribute("data-go-summary")) {
      if (busy) {
        return;
      }
      renderSummary();
      show("summary");
      return;
    }
    if (target.hasAttribute("data-go-printing")) {
      submitPrint();
      return;
    }
    if (target.hasAttribute("data-go-code") || target.hasAttribute("data-retry")) {
      show("code");
      return;
    }
    if (target.hasAttribute("data-back")) {
      var previous = {
        code: "welcome",
        documents: "code",
        preview: "documents",
        summary: "preview",
        printing: "summary",
        error: "code",
      };
      show(previous[visibleScreen()] || "welcome");
    }
  });

  window.addEventListener("resize", function () {
    if (!pdfDoc || busy) {
      return;
    }
    window.clearTimeout(fitTimer);
    fitTimer = window.setTimeout(function () {
      renderCurrentPage();
    }, 80);
  });

  clearPage();
  show("welcome");
  armIdle();
}
