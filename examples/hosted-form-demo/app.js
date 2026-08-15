(function () {
  "use strict";

  var config = window.__PAPERPILOT_DEMO__ || {};
  var grid = document.getElementById("template-grid");
  var listStatus = document.getElementById("template-status");
  var dialog = document.getElementById("form-dialog");
  var dialogTitle = document.getElementById("dialog-title");
  var sessionStatus = document.getElementById("session-status");
  var mountPoint = document.getElementById("document-form");
  var completion = document.getElementById("completion-message");
  var completionId = document.getElementById("completion-id");
  var resultStatus = document.getElementById("result-status");
  var resultFrame = document.getElementById("result-frame");
  var resultImage = document.getElementById("result-image");
  var resultDownload = document.getElementById("result-download");
  var externalUserInput = document.getElementById("external-user-id");
  var mountedForm = null;
  var sdkPromise = null;
  var activeResultToken = "";
  var demoTemplateNames = [
    "Boarding Pass1_Fixed",
    "Boarding Pass 5",
    "Three Way Flight Itinerary",
    "Return Flight Itinerary"
  ];
  var protectedTemplateName = "Boarding Pass1_Fixed";

  function displayName(name) {
    return String(name || "")
      .replace(/_Fixed$/, "")
      .replace(/_/g, " ")
      .replace(/([a-z])([0-9])/g, "$1 $2");
  }

  function loadSdk() {
    if (window.SharpToolz) return Promise.resolve(window.SharpToolz);
    if (sdkPromise) return sdkPromise;
    sdkPromise = new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = String(config.frontendUrl || "").replace(/\/$/, "") + "/embed/v1.js";
      script.onload = function () { resolve(window.SharpToolz); };
      script.onerror = function () { reject(new Error("The SharpToolz loader could not be loaded.")); };
      document.head.appendChild(script);
    });
    return sdkPromise;
  }

  function money(value) {
    var amount = Number(value);
    return Number.isFinite(amount) ? "₦" + amount.toLocaleString(undefined, { minimumFractionDigits: 2 }) : String(value || "Test mode");
  }

  function templateCard(template) {
    var article = document.createElement("article");
    article.className = "template-card";

    var art = document.createElement("div");
    art.className = "template-art";
    if (template.banner_url) {
      var image = document.createElement("img");
      image.src = template.banner_url;
      image.alt = template.name + " preview";
      image.loading = "lazy";
      art.appendChild(image);
    } else {
      var fallback = document.createElement("span");
      fallback.textContent = "Preview";
      art.appendChild(fallback);
    }

    var content = document.createElement("div");
    content.className = "template-content";
    var meta = document.createElement("div");
    meta.className = "template-meta";
    var category = document.createElement("span");
    category.textContent = template.name === protectedTemplateName
      ? "Protected Canvas preview"
      : "Real SharpToolz template";
    var price = document.createElement("b");
    price.textContent = money(template.price);
    meta.append(category, price);

    var title = document.createElement("h3");
    title.textContent = displayName(template.name);
    var button = document.createElement("button");
    button.type = "button";
    button.textContent = "Use this template";
    button.addEventListener("click", function () { openTemplate(template); });
    content.append(meta, title, button);
    article.append(art, content);
    return article;
  }

  async function loadTemplates() {
    try {
      var response = await fetch("/api/templates", { headers: { "Accept": "application/json" } });
      var payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Templates could not be loaded.");
      var templates = (payload.results || [])
        .filter(function (template) { return demoTemplateNames.includes(template.name); })
        .sort(function (left, right) {
          return demoTemplateNames.indexOf(left.name) - demoTemplateNames.indexOf(right.name);
        });
      if (!templates.length) throw new Error("Run the real template seed command first.");
      listStatus.hidden = true;
      templates.forEach(function (template) { grid.appendChild(templateCard(template)); });
    } catch (error) {
      listStatus.innerHTML = "";
      listStatus.textContent = error.message || "Templates could not be loaded.";
    }
  }

  async function openTemplate(template) {
    if (mountedForm) {
      mountedForm.destroy();
      mountedForm = null;
    }
    mountPoint.replaceChildren();
    completion.hidden = true;
    resultFrame.hidden = true;
    resultImage.removeAttribute("src");
    resultDownload.hidden = true;
    activeResultToken = "";
    sessionStatus.hidden = false;
    sessionStatus.className = "session-status";
    sessionStatus.innerHTML = '<span class="loader"></span> Preparing secure session…';
    dialogTitle.textContent = displayName(template.name);
    dialog.showModal();

    try {
      var externalUserId = externalUserInput.value.trim();
      if (!externalUserId) throw new Error("Add a user reference before opening the form.");
      var response = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({
          templateId: template.id,
          externalUserId: externalUserId,
          previewMode: template.name === protectedTemplateName ? "protected" : "standard"
        })
      });
      var session = await response.json();
      if (!response.ok) throw new Error(session.detail || "The secure session could not be created.");
      activeResultToken = session.result_token;
      var sdk = await loadSdk();
      sessionStatus.hidden = true;
      mountedForm = sdk.mount(mountPoint, {
        embedUrl: session.embed_url,
        autoResize: true,
        height: 760,
        borderRadius: "18px",
        onReady: function () { sessionStatus.hidden = true; },
        onComplete: function (result) {
          void showCreatedResult(result);
        },
        onError: function (result) {
          sessionStatus.hidden = false;
          sessionStatus.className = "session-status error";
          sessionStatus.textContent = result.message || "The hosted form reported an error.";
        }
      });
    } catch (error) {
      sessionStatus.hidden = false;
      sessionStatus.className = "session-status error";
      sessionStatus.textContent = error.message || "The secure session could not be created.";
    }
  }

  function wait(milliseconds) {
    return new Promise(function (resolve) { window.setTimeout(resolve, milliseconds); });
  }

  async function postJson(path, payload) {
    var response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(payload)
    });
    var data = await response.json();
    if (!response.ok) throw new Error(data.detail || "The finished document could not be prepared.");
    return data;
  }

  async function showCreatedResult(result) {
    completionId.textContent = result.documentId;
    resultStatus.className = "result-status";
    resultStatus.innerHTML = '<span class="loader"></span> Rendering the finished PNG…';
    completion.hidden = false;
    completion.scrollIntoView({ behavior: "smooth", block: "start" });

    try {
      var job = await postJson("/api/render", {
        resultToken: activeResultToken,
        sessionId: result.sessionId,
        documentId: result.documentId
      });
      for (var attempt = 0; attempt < 90 && ["queued", "running"].includes(job.status); attempt += 1) {
        await wait(750);
        job = await postJson("/api/render-status", {
          resultToken: activeResultToken,
          jobId: job.id
        });
      }
      if (job.status !== "completed" || !job.download_url) {
        throw new Error(job.error_code ? "Render failed: " + job.error_code : "The render did not finish in time.");
      }

      resultImage.onload = function () {
        resultFrame.hidden = false;
        resultStatus.textContent = "Finished document preview";
        completion.scrollIntoView({ behavior: "smooth", block: "start" });
      };
      resultImage.onerror = function () {
        resultStatus.textContent = "The PNG is ready. Open it using the button below.";
      };
      resultImage.src = job.download_url;
      resultDownload.href = job.download_url;
      resultDownload.hidden = false;
    } catch (error) {
      resultStatus.className = "result-status error";
      resultStatus.textContent = error.message || "The result could not be displayed.";
    }
  }

  function closeDialog() {
    if (mountedForm) {
      mountedForm.destroy();
      mountedForm = null;
    }
    dialog.close();
  }

  document.getElementById("close-dialog").addEventListener("click", closeDialog);
  document.getElementById("create-another").addEventListener("click", function () {
    closeDialog();
    document.getElementById("templates").scrollIntoView({ behavior: "smooth" });
  });
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) closeDialog();
  });
  loadTemplates();
})();
