"use strict";
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const ACTIVE = new Set(["queued","starting","context","focus","research","drafting","designing","validating","packaging"]);
  const names = {tavily:"Tavily",arxiv:"arXiv",github:"GitHub",producthunt:"Product Hunt",google_drive:"Google Drive",gmail:"Gmail",google_calendar:"Google Calendar",filesystem:"Filesystem",twitter:"Twitter / X"};
  const descriptions = {tavily:"Web news, launch discovery, and public community fallback.",arxiv:"AI research papers, abstracts, publication dates and PDF links.",github:"Open-source repositories, descriptions and star snapshots.",producthunt:"AI product launches through authorized Product Hunt API access.",google_drive:"Local read-only MCP for performance data, topic ideas and editorial guidance.",gmail:"Local read-only MCP for newsletter feedback from your authorized mailbox.",google_calendar:"Local read-only MCP for publication events and scheduling context.",filesystem:"Restricted read/write access to your configured workspace.",twitter:"Official X MCP full-archive post search using a read-only app Bearer token. Tavily remains the public-web fallback."};
  const routeDescriptions = {ollama:"Every model call stays with Ollama. Research still uses online MCPs.",groq:"Focus planning, editorial writing and HTML design all use Groq.",hybrid:"Small tasks: Ollama. Drafting and HTML design: the larger Groq model.",gemini:"Every model call uses Gemini with its configured quota-aware key pool.",openai:"Every model call uses your configured OpenAI model and credentials.",claude:"Every model call uses Claude through the Anthropic Messages API."};
  const labels = {queued:"Queued",starting:"Starting",context:"Loading context",focus:"Planning focus",research:"Researching",drafting:"Writing draft",designing:"Designing email",validating:"Validating",packaging:"Saving exports",awaiting_review:"Awaiting review",approved:"Approved",failed:"Failed",cancelled:"Cancelled",interrupted:"Interrupted",needs_packaging:"Needs Phase 3"};
  const researchFiles = {web:"web_research.md",arxiv:"arxiv_research.md",github:"github_research.md",producthunt:"product_hunt_research.md",twitter:"twitter_research.md"};
  const state = {config:null,settings:null,runs:[],quotas:null,token:"",run:null,tab:"preview",draftOriginal:"",draftDirty:false,settingsDirty:false,doctor:null,loading:false,polling:false,routeVersion:0};
  let toastTimer;

  function toast(message, error = false) {
    const item = document.createElement("div"); item.className = "toast" + (error ? " error" : ""); item.textContent = message;
    $("#toast-region").replaceChildren(item); clearTimeout(toastTimer); toastTimer = setTimeout(() => item.remove(), 6500);
  }
  function showError(error) {
    if (error.message === "AUTH_REQUIRED") return;
    $("#global-error-text").textContent = error.message || String(error); $("#global-error").hidden = false;
    toast(error.message || "An operation failed. Check the error message.", true);
  }
  function online(value) {
    const node = $("#connection-status"); node.classList.toggle("offline", !value);
    node.innerHTML = '<span class="dot"></span>' + (value ? "Local server connected" : "Server unavailable");
  }
  async function api(path, options = {}) {
    const {raw = false, text = false, timeout = 30000, ...init} = options;
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), timeout);
    const headers = {"X-Requested-With":"newsletter-studio",...(state.token ? {Authorization:"Bearer " + state.token} : {}),...(init.headers || {})};
    if (init.body && typeof init.body !== "string") { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(init.body); }
    try {
      const response = await fetch(path, {...init, headers, signal:controller.signal});
      online(true);
      if (response.status === 401) { if (!$("#auth-dialog").open) $("#auth-dialog").showModal(); throw new Error("AUTH_REQUIRED"); }
      if (!response.ok) {
        let detail = `Request failed (${response.status})`;
        try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body); } catch (_) { /* no response body */ }
        const error = new Error(detail); error.status = response.status; throw error;
      }
      if (raw) return response;
      return text ? response.text() : response.json();
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The request timed out. Check the edition status before retrying; the server may still be working.");
      if (error instanceof TypeError) { online(false); throw new Error("Cannot reach the local server. Keep newsletter serve running and use Refresh to reconnect."); }
      throw error;
    } finally { clearTimeout(timer); }
  }
  function on(selector, event, handler) {
    const el = $(selector); if (!el) return;
    el.addEventListener(event, ev => Promise.resolve(handler(ev)).catch(showError));
  }
  async function busyButton(button, label, operation) {
    if (button.disabled) return;
    const old = button.innerHTML; button.disabled = true; button.textContent = label;
    try { return await operation(); } finally { button.innerHTML = old; button.disabled = false; if (state.config) updateRunForm(); if (state.run) renderReviewStatus(); }
  }
  function badge(status) {
    const tone = status === "approved" || status === "ready" || status === "configured" ? "success" : status === "failed" || status === "authorization_failed" ? "error" : ["awaiting_review","needs_setup","needs_packaging","blocked"].includes(status) ? "warning" : "info";
    return `<span class="badge ${tone}">${esc(labels[status] || status.replaceAll("_"," "))}</span>`;
  }
  function dateTime(value) { try { return new Date(value).toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}); } catch (_) { return value; } }
  function getNested(object,path) { return path.split(".").reduce((v,k) => v?.[k],object); }
  function setNested(object,path,value) { const parts=path.split("."); let node=object; parts.slice(0,-1).forEach(k => node=node[k] ??= {}); node[parts.at(-1)]=value; }
  function hasActiveRun() { return state.runs.some(r => ACTIVE.has(r.status)); }

  async function loadAll({fill = true} = {}) {
    const [config,settings,runs,quotas] = await Promise.all([api("/api/config"),api("/api/settings"),api("/api/runs"),api("/api/quotas")]);
    state.config=config; state.settings=settings; state.runs=runs; state.quotas=quotas;
    renderDashboard(); renderEditions(); renderIntegrations(); renderPools(); renderCredentialState();
    if (fill && !state.settingsDirty) fillSettings();
    $("#edition-count").textContent = runs.length;
  }
  function runTable(rows) {
    if (!rows.length) return '<div class="empty-state"><div class="empty-symbol">n.</div><h3>A blank page, in the best way.</h3><p>Generate an offline demo to see the entire workflow without connecting an account.</p><a href="#/dashboard" class="text-link">Create an edition &rarr;</a></div>';
    return '<table class="run-list"><thead><tr><th>EDITION</th><th>STATUS</th><th class="hide-mobile">MODE</th><th></th></tr></thead><tbody>' + rows.map(r => `<tr><td><a class="run-title" href="#/review/${esc(r.id)}">${esc(r.details.title || (r.demo ? "Offline demonstration" : "New newsletter edition"))}</a><small>${esc(dateTime(r.created_at))}${r.demo ? " &middot; SYNTHETIC DEMO" : ""}</small></td><td>${badge(r.status)}</td><td class="hide-mobile">${esc(r.provider)}</td><td><a class="text-link" href="#/review/${esc(r.id)}">Open &rarr;</a></td></tr>`).join("") + '</tbody></table>';
  }
  function renderDashboard() {
    if (!state.config) return;
    const c=state.config;
    $("#stat-editions").textContent=state.runs.length;
    $("#stat-review").textContent=state.runs.filter(r=>r.status==="awaiting_review").length;
    $("#research-window").textContent=`Last ${c.lookback_days} days`;
    $("#workspace-path").textContent=c.workspace;
    $("#engine-note").textContent=`Phase 3: ${c.phase3.mode === "llm" ? "LLM builder" : "Jinja (explicit)"} / Jinja fallback ${c.features.allow_jinja_fallback ? "enabled" : "off"}`;
    $("#connection-mini").innerHTML=c.integrations.map(i=>`<div class="mini-service ${i.state==="configured"?"ready":""}" title="${esc(i.state)}"><span class="dot"></span>${esc(names[i.name])}</div>`).join("");
    $("#recent-runs").innerHTML=runTable(state.runs.slice(0,4));
    if (!$("#run-mode").dataset.userChoice) $("#run-mode").value=c.provider;
    updateRunForm();
  }
  function updateRunForm() {
    const mode=$("#run-mode").value, demo=$("#run-type").value==="demo";
    $("#route-description").textContent=routeDescriptions[mode];
    $("#run-note").textContent=demo ? "Demo uses clearly labeled synthetic sources and model responses. No API calls or charges." : "Live research uses your configured credentials. Review claims and sources before publishing; provider charges may apply.";
    $("#run-note").className="notice " + (demo?"info":"warning");
    $("#generate-button").innerHTML=(hasActiveRun()?"An edition is running":demo?"Generate demo edition":"Generate live edition")+' <span aria-hidden="true">&rarr;</span>';
    $("#generate-button").disabled=hasActiveRun();
  }
  function renderEditions() {
    const search=$("#edition-search").value.toLowerCase(),filter=$("#edition-filter").value;
    $("#all-runs").innerHTML=runTable(state.runs.filter(r=>(`${r.id} ${r.details.title || ""}`).toLowerCase().includes(search) && (filter==="all" || (filter==="demo"?r.demo:r.status===filter))));
  }
  function renderIntegrations() {
    if (!state.config) return;
    $("#integration-grid").innerHTML=state.config.integrations.map(i=>{
      const health=state.doctor?.[i.name]; const status=health?.state || i.state;
      const detail=health ? (typeof health.detail==="string"?health.detail:`${health.tools?.length || 0} tools discovered`) : i.required?"Required for this workflow":"Optional integration";
      return `<article class="panel integration-card"><div class="card-top"><span class="integration-icon">${esc({tavily:"Ta",arxiv:"aX",github:"Gh",producthunt:"Ph",google_drive:"Dr",gmail:"Gm",google_calendar:"Ca",filesystem:"Fs",twitter:"X"}[i.name])}</span>${badge(status)}</div><h3>${esc(names[i.name])}</h3><p>${esc(descriptions[i.name])}</p>${i.tavily_fallback?'<span class="badge subtle">TAVILY FALLBACK ON</span>':""}<p class="integration-detail">${esc(detail)}</p><div class="card-footer"><span>${esc(i.transport)} MCP</span><label class="sr-only" for="toggle-${i.name}">Enable ${esc(names[i.name])}</label><input type="checkbox" class="switch integration-toggle" id="toggle-${i.name}" data-server="${i.name}" ${state.settings.mcps[i.name].enabled?"checked":""} ${i.name==="filesystem"?"disabled":""}></div></article>`;
    }).join("");
    $("#fallback-producthunt").checked=state.settings.research_fallback.producthunt_to_tavily;
    $("#fallback-twitter").checked=state.settings.research_fallback.twitter_to_tavily;
  }
  function fillSettings() {
    $$('[data-setting]').forEach(el=>{const value=getNested(state.settings,el.dataset.setting); if(el.type==="checkbox") el.checked=Boolean(value); else if(el.dataset.type==="lines") el.value=(value||[]).join("\n"); else el.value=value??"";});
    $("#quota-json").value=JSON.stringify(state.settings.llm.quotas,null,2);
    $("#producthunt-oauth-state").textContent=state.config.oauth?.producthunt?"Connected through Product Hunt OAuth.":"Not connected. Product launches use the labeled Tavily fallback.";
    $("#google-oauth-state").textContent=state.config.oauth?.google?"Connected. Local Drive, Gmail and Calendar MCPs use a refreshable read-only authorization.":"Not connected. Google private context is omitted.";
    updateJinjaField();
  }
  function updateJinjaField() {
    const flag=$("#jinja-flag").checked; $('#phase3-mode option[value="jinja"]').disabled=!flag;
    if(!flag && $("#phase3-mode").value==="jinja") $("#phase3-mode").value="llm";
  }
  function renderPools() {
    const provider=$("#pool-provider").value,entries=state.quotas?.providers?.[provider] || [];
    $("#pool-summary").innerHTML=`<p class="hint">${entries.length} configured key${entries.length===1?"":"s"} for ${esc(provider)}. Usage totals below are recorded by this app, not your provider's billing balance.</p>`+entries.map(e=>`<div class="pool-entry"><strong>${esc(e.label)}</strong>${badge(e.state)}<span>${esc(e.quota_group)} / ${e.requests} calls / ${e.tokens.toLocaleString()} tokens${e.cooldown_seconds?` / ${e.cooldown_seconds}s cooldown`:""}</span></div>`).join("");
  }
  function renderCredentialState() { const present=state.config?.credential_presence?.[$("#credential-name").value]; $("#credential-state").textContent=present?"A credential is configured. Its value is never displayed.":"No credential is configured for this service."; }

  async function navigate() {
    const version=++state.routeVersion;
    const hash=location.hash.startsWith("#/")?location.hash.slice(2):"dashboard";
    const [page,id]=hash.split("/"); const route=["dashboard","editions","integrations","settings","review","guide"].includes(page)?page:"dashboard";
    $$(".page").forEach(el=>el.hidden=el.id!=="page-"+route);
    $$('[data-nav]').forEach(a=>{const active=a.dataset.nav===(route==="review"?"editions":route);a.classList.toggle("active",active);if(active)a.setAttribute("aria-current","page");else a.removeAttribute("aria-current");});
    $("#page-name").textContent={dashboard:"Overview",editions:"Editions",integrations:"MCP connections",settings:"Settings & keys",review:"Edition review",guide:"Setup guide"}[route];
    $("#sidebar").classList.remove("open");$("#menu-toggle").setAttribute("aria-expanded","false");
    if(route==="review") {
      if(!id || !/^\d{8}T\d{6}-[a-f0-9]{8}$/.test(id)) throw new Error("Invalid edition address.");
      const run=await api("/api/runs/"+id);if(version!==state.routeVersion)return;
      const changed=state.run?.id!==id;state.run=run;
      if(changed){state.tab="preview";state.draftDirty=false;state.draftOriginal="";$("#email-preview").srcdoc="";}
      renderReviewStatus(); await showTab(state.tab);
    }
  }
  function renderReviewStatus() {
    const r=state.run;if(!r)return;const running=ACTIVE.has(r.status),complete=["awaiting_review","approved"].includes(r.status);
    $("#review-badges").innerHTML=badge(r.status)+(r.demo?'<span class="badge subtle">SYNTHETIC DEMO</span>':`<span class="badge subtle">${esc(r.provider)}</span>`);
    $("#review-title").textContent=r.details.title || "Preparing your next edition";
    $("#review-meta").textContent=`${dateTime(r.created_at)} / ${r.id}`;
    $("#cancel-run").hidden=!running;
    $("#retry-phase3").disabled=running || !r.details.has_draft;
    $("#retry-phase3").title="Uses the saved model mode: "+(state.config?.provider||"ollama");
    $("#approve-run").disabled=r.demo || r.status!=="awaiting_review";
    $$('[data-download]').forEach(b=>b.disabled=b.dataset.download!=="bundle"&&!complete);
    let message=running?`Stage: ${labels[r.status]}. Progress is saved on this computer. You may navigate to another page; keep the server running.`:r.status==="approved"?"Editorial approval recorded for these artifacts. No email was sent.":r.status==="needs_packaging"?"The Markdown was edited. Earlier exports are stale. Run Phase 3 again before previewing or approving.":complete?(r.demo?"Demo complete: every source is synthetic and model responses are fixtures. This edition cannot be approved for publication.":"Ready for editorial review. Inspect the research and email, then approve or edit the draft."):(r.details.error||"Execution stopped. Completed files are retained; retry Phase 3 when a draft is available.");
    const status=$("#review-status");status.textContent=message;status.className="notice "+(["failed","interrupted"].includes(r.status)?"error":complete?"success":"info");
    let stage=["drafting"].includes(r.status)?1:["designing","validating","packaging"].includes(r.status)?2:0;
    if(complete)stage=3;else if(!running && r.details.has_draft)stage=2;
    $("#progress-track").innerHTML=["Research & collect","Write the edition","Design & validate"].map((name,i)=>`<div class="progress-step ${i<stage?"complete":i===stage&&running?"active":""}"><span>${i<stage?"OK":"0"+(i+1)}</span><strong>${name}</strong></div>`).join("");
    $("#save-draft").disabled=running || !state.draftDirty;
  }
  async function file(name, json=false) {
    const text=await api(`/api/runs/${state.run.id}/files/${name}`,{text:true});return json?JSON.parse(text):text;
  }
  async function showTab(tab) {
    state.tab=tab;
    $$('[data-tab]').forEach(b=>b.setAttribute("aria-selected",String(b.dataset.tab===tab)));
    $$(".tab-panel").forEach(el=>el.hidden=el.id!=="tab-"+tab);
    const id=state.run?.id;if(!id)return;
    const complete=["awaiting_review","approved"].includes(state.run.status);
    try {
      if(tab==="preview") {
        $("#preview-empty").hidden=complete;$("#email-preview").hidden=!complete;
        if(complete) {const content=await file("newsletter.html");if(state.run.id===id)$("#email-preview").srcdoc=content;}
      } else if(tab==="draft") {
        if(!state.draftDirty) {const content=await file("final_draft_newsletter.md");if(state.run.id!==id)return;$("#draft-editor").value=content;state.draftOriginal=content;}
        $("#draft-editor").disabled=ACTIVE.has(state.run.status);
        $("#draft-status").textContent=state.draftDirty?"Unsaved changes":"Saved Markdown. Nine section headings must be retained.";
      } else if(tab==="research") await renderResearch();
      else if(tab==="checks") await renderChecks();
      else if(tab==="activity") await renderActivity();
    } catch(error) {
      if(error.status===404 || error.status===409) {
        if(tab==="draft") {$("#draft-editor").value="";$("#draft-editor").disabled=true;$("#draft-status").textContent="The draft is not available yet.";}
        else if(tab==="checks") $("#checks-content").innerHTML='<div class="empty-state"><p>Validation results appear after Phase 3 completes.</p></div>';
        else if(tab==="research"){$("#source-cards").innerHTML='<div class="empty-state"><p>Research files are not available yet.</p></div>';$("#research-raw").textContent="";}
        else if(tab==="activity"){$("#activity-content").textContent="Execution events will appear here.";}
        return;
      }
      throw error;
    }
  }
  async function renderResearch() {
    const channel=$("#research-channel").value; const research=await file("research.json",true);
    const status=research.channels[channel]||{};
    $("#research-notice").textContent=status.reason || `${status.count || 0} source records. Retrieved via ${names[status.retrieved_via]||status.retrieved_via||channel}.`;
    const records=research.sources.filter(s=>s.channel===channel);
    $("#source-cards").innerHTML=records.length?records.map(s=>`<article class="source-card"><h3>${esc(s.title)}</h3><a class="source-url" href="${/^https?:\/\//.test(s.url)?esc(s.url):"#"}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a><p>${esc(s.summary)}</p><div class="source-meta"><span>${esc(s.id)}</span><span>${s.published_at?"Source date: "+esc(s.published_at.slice(0,10)):"Publication date not verified"}</span>${s.metadata.retrieval_method?`<span>${esc(s.metadata.retrieval_method)}</span>`:""}</div></article>`).join(""):'<div class="empty-state"><p>No qualifying sources. No content was invented.</p></div>';
    $("#research-raw").textContent=await file(researchFiles[channel]);
  }
  async function renderChecks() {
    const report=await file("quality_report.json",true);const phase=report.phase3||{};
    $("#checks-content").innerHTML=`<div class="check-grid"><div class="check-card"><strong>${esc(phase.engine||"Not recorded")}</strong><span>Actual Phase 3 engine</span></div><div class="check-card"><strong>${phase.safe_html?"Passed":"Not recorded"}</strong><span>HTML safety and nine-section checks</span></div><div class="check-card"><strong>${phase.content_preservation_checked?"Checked":"Not enabled"}</strong><span>Content preservation against Markdown</span></div></div><div class="notice warning">These checks are not independent fact verification or a Gmail/Outlook compatibility test. Review before publishing.</div>${(report.warnings||[]).map(w=>`<p class="hint">${esc(w)}</p>`).join("")}<details><summary>Complete quality report</summary><pre>${esc(JSON.stringify(report,null,2))}</pre></details><p class="hint">Phase 3 input SHA-256: <code>${esc(phase.input_sha256||"not recorded")}</code></p>`;
  }
  async function renderActivity() {
    const r=state.run;let manifest={};try{manifest=await file("manifest.json",true);}catch(error){if(error.status!==404)throw error;}
    const events=r.events||[];
    $("#activity-content").innerHTML=events.map(e=>`<div class="activity-event"><time>${esc(dateTime(e.created_at))}</time><div><strong>${esc(labels[e.phase]||e.phase)}</strong><p>${esc(e.message)}</p></div></div>`).join("") + ((manifest.llm_audit||[]).length?'<h3>Model request audit</h3><div style="overflow:auto"><table class="audit-table"><thead><tr><th>Task</th><th>Provider / model</th><th>Key / group</th><th>Result</th></tr></thead><tbody>'+manifest.llm_audit.map(a=>`<tr><td>${esc(a.task||"generation")}</td><td>${esc(a.provider)}<br>${esc(a.model)}</td><td>${esc(a.key_label||"local")}<br>${esc(a.quota_group||"local")}</td><td>${esc(a.status)}</td></tr>`).join("")+'</tbody></table></div>':'<p class="hint">No live model calls recorded. Demo mode uses synthetic fixtures.</p>');
  }
  async function startRun(demo) {
    await busyButton($("#generate-button"),"Starting...",async()=>{
      const result=await api("/api/runs",{method:"POST",body:{demo,provider:$("#run-mode").value}});
      state.runs=await api("/api/runs");renderDashboard();location.hash="/review/"+result.run_id;toast(demo?"Offline demo started.":"Live workflow started. No email will be sent.");
    });
  }
  async function download(name) {
    const path=name==="bundle"?`/api/runs/${state.run.id}/download`:`/api/runs/${state.run.id}/files/${name}?download=true`;
    const response=await api(path,{raw:true,timeout:120000});const blob=await response.blob();const url=URL.createObjectURL(blob);
    const a=document.createElement("a");a.href=url;a.download=name==="bundle"?`newsletter-${state.run.id}.zip`:name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);toast("Download prepared.");
  }

  on("#run-form","submit",async e=>{e.preventDefault();if($("#run-type").value==="demo")return startRun(true);$("#live-summary").textContent=routeDescriptions[$("#run-mode").value]+" Your configured research integrations will be used.";$("#live-dialog").showModal();});
  on("#live-form","submit",async e=>{e.preventDefault();$("#live-dialog").close();await startRun(false);});
  on("#live-dismiss","click",()=>$("#live-dialog").close());
  on("#run-mode","change",()=>{$("#run-mode").dataset.userChoice="true";updateRunForm();});
  on("#run-type","change",updateRunForm);
  on("#refresh-all","click",e=>busyButton(e.currentTarget,"Refreshing...",async()=>{state.doctor=null;await loadAll();await navigate();toast("Workspace refreshed.");}));
  on("#dismiss-error","click",()=>$("#global-error").hidden=true);
  on("#menu-toggle","click",()=>{const open=$("#sidebar").classList.toggle("open");$("#menu-toggle").setAttribute("aria-expanded",String(open));});
  on("#copy-workspace","click",async()=>{try{await navigator.clipboard.writeText(state.config.workspace);toast("Workspace path copied.");}catch(_){toast("Clipboard unavailable. Select and copy the displayed path.",true);}});
  on("#edition-search","input",renderEditions);on("#edition-filter","change",renderEditions);
  on("#doctor-button","click",e=>busyButton(e.currentTarget,"Checking nine MCPs...",async()=>{const result=await api("/api/doctor",{method:"POST",timeout:180000});state.doctor=result.servers;$("#doctor-scope").textContent=result.scope;renderIntegrations();toast("MCP connection check complete. This was not a live API authorization test.");}));
  on("#integration-grid","change",async e=>{const input=e.target.closest("[data-server]");if(!input)return;const name=input.dataset.server,value=input.checked;input.disabled=true;try{await api("/api/settings",{method:"PUT",body:{mcps:{[name]:{enabled:value}}}});state.doctor=null;await loadAll({fill:false});toast(`${names[name]} ${value?"enabled":"disabled"}.`);}catch(error){input.checked=!value;throw error;}finally{input.disabled=false;}});
  on("#save-fallbacks","click",e=>busyButton(e.currentTarget,"Saving...",async()=>{await api("/api/settings",{method:"PUT",body:{research_fallback:{producthunt_to_tavily:$("#fallback-producthunt").checked,twitter_to_tavily:$("#fallback-twitter").checked}}});await loadAll({fill:false});toast("Tavily fallback preferences saved.");}));
  on("#settings-form","input",()=>{state.settingsDirty=true;$("#settings-save-status").textContent="Unsaved preferences";});
  on("#jinja-flag","change",updateJinjaField);
  on("#settings-form","submit",async e=>{e.preventDefault();await busyButton($("#save-settings"),"Saving...",async()=>{const changes={};$$('[data-setting]').forEach(el=>{let value=el.type==="checkbox"?el.checked:el.type==="number"||el.dataset.type==="number"?Number(el.value):el.dataset.type==="lines"?el.value.split("\n").map(s=>s.trim()).filter(Boolean):el.value.trim();setNested(changes,el.dataset.setting,value);});try{changes.llm.quotas=JSON.parse($("#quota-json").value||"{}");}catch(_){throw new Error("Quota configuration must be valid JSON.");}await api("/api/settings",{method:"PUT",body:changes});state.settingsDirty=false;delete $("#run-mode").dataset.userChoice;await loadAll();$("#settings-save-status").textContent="Preferences saved locally.";toast("Preferences saved. Existing editions remain in their original workspace.");});});
  on("#model-check","click",e=>busyButton(e.currentTarget,"Testing saved model...",async()=>{if(!window.confirm("This makes a real model request using the SAVED configuration and may incur charges. Continue?"))return;const result=await api("/api/model-check",{method:"POST",body:{provider:state.config.provider},timeout:3600000});$("#model-check-result").hidden=false;$("#model-check-result").textContent=JSON.stringify(result,null,2);toast("Configured model responded successfully.");state.quotas=await api("/api/quotas");renderPools();}));
  on("#pool-provider","change",renderPools);
  on("#pool-form","submit",async e=>{e.preventDefault();const lines=$("#pool-values").value.split("\n").map(l=>l.trim()).filter(Boolean);if(!lines.length)throw new Error("Enter at least one key, or use Clear this pool.");const keys=lines.map(line=>{const divider=line.indexOf("|");return divider<0?{value:line,quota_group:"default"}:{quota_group:line.slice(0,divider).trim(),value:line.slice(divider+1).trim()};});await busyButton($("#save-pool"),"Saving...",async()=>{await api("/api/key-pools",{method:"POST",body:{provider:$("#pool-provider").value,keys}});$("#pool-values").value="";await loadAll({fill:false});toast("Key pool saved. Shared quotas were not reset.");});});
  on("#clear-pool","click",async()=>{if(!window.confirm("Remove this provider's model credentials from this project's .env?"))return;await api("/api/key-pools",{method:"POST",body:{provider:$("#pool-provider").value,keys:[]}});await loadAll({fill:false});toast("Key pool cleared.");});
  on("#credential-name","change",renderCredentialState);
  on("#credential-form","submit",async e=>{e.preventDefault();const value=$("#credential-value").value.trim();if(!value)throw new Error("Enter a credential, or use Clear credential.");await api("/api/credentials",{method:"POST",body:{name:$("#credential-name").value,value}});$("#credential-value").value="";state.doctor=null;await loadAll({fill:false});toast("Credential saved locally.");});
  on("#clear-credential","click",async()=>{if(!window.confirm("Remove this service credential from the project?"))return;await api("/api/credentials",{method:"POST",body:{name:$("#credential-name").value,value:""}});await loadAll({fill:false});toast("Credential cleared.");});
  async function connectOAuth(provider) { const result=await api(`/api/oauth/${provider}/start`,{method:"POST",timeout:60000});window.open(result.authorization_url,`newsletter-${provider}-oauth`,`popup,width=720,height=780`);toast("Complete authorization in the new browser window, then press Refresh."); }
  on("#connect-producthunt","click",e=>busyButton(e.currentTarget,"Opening Product Hunt...",()=>connectOAuth("producthunt")));
  on("#connect-google","click",e=>busyButton(e.currentTarget,"Opening Google...",()=>connectOAuth("google")));
  on("#disconnect-producthunt","click",async()=>{if(!window.confirm("Disconnect Product Hunt from this project?"))return;await api("/api/oauth/producthunt/disconnect",{method:"POST"});state.doctor=null;await loadAll();toast("Product Hunt disconnected.");});
  on("#disconnect-google","click",async()=>{if(!window.confirm("Disconnect Google Workspace from this project?"))return;await api("/api/oauth/google/disconnect",{method:"POST"});state.doctor=null;await loadAll();toast("Google Workspace disconnected.");});
  $$('[data-tab]').forEach(button=>on(`[data-tab="${button.dataset.tab}"]`,"click",()=>showTab(button.dataset.tab)));
  $$('[data-device]').forEach(button=>on(`[data-device="${button.dataset.device}"]`,"click",()=>{$('[data-device="desktop"]').classList.toggle("selected",button.dataset.device==="desktop");$('[data-device="mobile"]').classList.toggle("selected",button.dataset.device==="mobile");$$('[data-device]').forEach(b=>b.setAttribute("aria-pressed",String(b===button)));$("#email-preview").classList.toggle("mobile",button.dataset.device==="mobile");}));
  $$('[data-download]').forEach(button=>on(`[data-download="${button.dataset.download}"]`,"click",()=>download(button.dataset.download)));
  on("#research-channel","change",renderResearch);
  on("#draft-editor","input",()=>{state.draftDirty=$("#draft-editor").value!==state.draftOriginal;$("#draft-status").textContent=state.draftDirty?"Unsaved changes":"No changes";renderReviewStatus();});
  on("#save-draft","click",e=>busyButton(e.currentTarget,"Saving...",async()=>{await api(`/api/runs/${state.run.id}/draft`,{method:"PUT",body:{markdown:$("#draft-editor").value}});state.draftOriginal=$("#draft-editor").value;state.draftDirty=false;state.run=await api("/api/runs/"+state.run.id);renderReviewStatus();$("#draft-status").textContent="Saved. Run Phase 3 again to regenerate exports.";toast("Draft saved. Previous exports and approval are invalidated.");}));
  on("#retry-phase3","click",e=>busyButton(e.currentTarget,"Starting Phase 3...",async()=>{if(state.draftDirty)throw new Error("Save or discard your Markdown edits before retrying Phase 3.");if(!state.run.demo&&!window.confirm(`Run Phase 3 with the saved ${state.config.provider} mode? This can consume model quota.`))return;await api(`/api/runs/${state.run.id}/repackage`,{method:"POST",body:{provider:state.config.provider}});state.run=await api("/api/runs/"+state.run.id);renderReviewStatus();await showTab("preview");toast("Phase 3 restarted from the saved Markdown. Research is not repeated.");}));
  on("#cancel-run","click",e=>busyButton(e.currentTarget,"Cancelling...",async()=>{await api(`/api/runs/${state.run.id}/cancel`,{method:"POST",timeout:90000});state.run=await api("/api/runs/"+state.run.id);renderReviewStatus();await loadAll({fill:false});toast("Run cancelled. Completed stage files were retained.");}));
  on("#approve-run","click",()=>$("#approval-dialog").showModal());
  on("#approval-dismiss","click",()=>$("#approval-dialog").close());
  on("#approval-form","submit",async e=>{e.preventDefault();await api(`/api/runs/${state.run.id}/approve`,{method:"POST",body:{reviewer:$("#reviewer-name").value.trim()},timeout:120000});$("#approval-dialog").close();state.run=await api("/api/runs/"+state.run.id);renderReviewStatus();await loadAll({fill:false});toast("Editorial approval recorded. No email was sent.");});
  on("#auth-form","submit",async e=>{e.preventDefault();state.token=$("#auth-token").value;try{await loadAll();$("#auth-dialog").close();$("#auth-token").value="";await navigate();}catch(error){if(error.message==="AUTH_REQUIRED")toast("Incorrect dashboard token.",true);else throw error;}});
  $("#auth-dialog").addEventListener("cancel",e=>e.preventDefault());
  window.addEventListener("hashchange",()=>navigate().catch(showError));
  window.addEventListener("beforeunload",e=>{if(state.draftDirty||state.settingsDirty){e.preventDefault();e.returnValue="";}});
  document.addEventListener("click",e=>{const a=e.target.closest('a[href^="#/review/"]');if(a&&state.draftDirty&&a.hash!==location.hash&&!window.confirm("Leave this edition and discard unsaved Markdown edits?"))e.preventDefault();});
  let ticks=0;
  setInterval(async()=>{
    if(state.polling||!state.config)return;state.polling=true;
    try {
      if(state.run && ACTIVE.has(state.run.status)) {
        const prior=state.run.status;state.run=await api("/api/runs/"+state.run.id);
        renderReviewStatus();
        if(prior!==state.run.status){if(!state.draftDirty)await showTab(state.tab);if(!ACTIVE.has(state.run.status))await loadAll({fill:false});}
      }
      if(++ticks%10===0){state.runs=await api("/api/runs");renderDashboard();renderEditions();$("#edition-count").textContent=state.runs.length;}
    } catch(error){if(error.message!=="AUTH_REQUIRED")online(false);} finally {state.polling=false;}
  },1200);
  (async()=>{try{await loadAll();await navigate();}catch(error){showError(error);}})();
})();
