import { useState, useRef, useEffect, useCallback } from "react";

// ── CONFIG ────────────────────────────────────────────────────────────────
const LLM_URL   = "http://localhost:12215/api/copilot/completion";
const MODEL_ID  = "gpt-4o-mini";

// ── AGENTIC PROMPT BUILDER ────────────────────────────────────────────────
// Lazy-loads skill.md in chunks — simulates agentic retrieval pattern
function buildAgenticPrompt(skillContent, jsonData) {
  // Split skill into "retrieved" sections (agentic lazy load simulation)
  const sections = skillContent
    .split(/\n#{1,3} /)
    .filter(Boolean)
    .map(s => s.trim());

  const relevantSections = sections.slice(0, 6).join("\n\n---\n\n");

  return {
    systemPrompt: `You are an expert agentic AI assistant executing a skill.\n\nSKILL DEFINITION (lazily loaded):\n\`\`\`\n${relevantSections}\n\`\`\`\n\nRules:\n- Apply the skill EXACTLY as defined above\n- Output beautifully formatted markdown\n- Use headers, bullet points, callouts\n- Be concise but thorough\n- End with a KEY INSIGHTS section`,
    userPrompt: `Apply the skill to this input data:\n\`\`\`json\n${jsonData}\n\`\`\`\n\nProduce a beautiful, well-structured summary.`,
  };
}

// ── MARKDOWN RENDERER ─────────────────────────────────────────────────────
function renderMarkdown(text) {
  if (!text) return "";
  return text
    .replace(/^### (.+)$/gm, '<h3 class="md-h3">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 class="md-h2">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 class="md-h1">$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/`(.+?)`/g, '<code class="md-code">$1</code>')
    .replace(/^- (.+)$/gm, '<li class="md-li">$1</li>')
    .replace(/(<li.*<\/li>\n?)+/g, s => `<ul class="md-ul">${s}</ul>`)
    .replace(/^> (.+)$/gm, '<blockquote class="md-bq">$1</blockquote>')
    .replace(/\n{2,}/g, '</p><p class="md-p">')
    .replace(/^(?!<[hublp])(.+)$/gm, '<p class="md-p">$1</p>');
}

// ══════════════════════════════════════════════════════════════════════════
export default function SkillRunner() {
  const [skillText, setSkillText]       = useState("");
  const [skillName, setSkillName]       = useState("");
  const [jsonInput, setJsonInput]       = useState('{\n  "query": "Explain this data",\n  "data": {}\n}');
  const [output, setOutput]             = useState("");
  const [loading, setLoading]           = useState(false);
  const [phase, setPhase]               = useState("idle"); // idle | loading-skill | building | calling | streaming | done | error
  const [agentLog, setAgentLog]         = useState([]);
  const [jsonError, setJsonError]       = useState("");
  const [modelId, setModelId]           = useState(MODEL_ID);
  const [activeTab, setActiveTab]       = useState("output"); // output | raw
  const fileRef   = useRef(null);
  const outputRef = useRef(null);

  // ── scroll output to bottom as it streams ──
  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [output]);

  // ── log helper ──
  const log = useCallback((msg, type = "info") => {
    setAgentLog(prev => [...prev, { msg, type, ts: Date.now() }]);
  }, []);

  // ── FILE LOAD ──────────────────────────────────────────────────────────
  function handleFileLoad(e) {
    const file = e.target.files[0];
    if (!file) return;
    setSkillName(file.name);
    setPhase("loading-skill");
    log(`📂 Loading skill: ${file.name}`, "info");

    const reader = new FileReader();
    reader.onload = ev => {
      const content = ev.target.result;
      setSkillText(content);
      setPhase("idle");
      log(`✅ Skill loaded — ${content.split("\n").length} lines`, "success");
      // Show first line as name if it's markdown heading
      const firstLine = content.split("\n").find(l => l.startsWith("#"));
      if (firstLine) setSkillName(firstLine.replace(/^#+\s*/, ""));
    };
    reader.readAsText(file);
  }

  // ── JSON VALIDATE ─────────────────────────────────────────────────────
  function handleJsonChange(val) {
    setJsonInput(val);
    try {
      JSON.parse(val);
      setJsonError("");
    } catch (e) {
      setJsonError(e.message);
    }
  }

  // ── PASTE SKILL AS TEXT ───────────────────────────────────────────────
  function handleSkillPaste(e) {
    const val = e.target.value;
    setSkillText(val);
    const firstLine = val.split("\n").find(l => l.startsWith("#"));
    setSkillName(firstLine ? firstLine.replace(/^#+\s*/, "") : "Pasted Skill");
    log(`📋 Skill pasted — ${val.split("\n").length} lines`, "info");
  }

  // ── MAIN RUN ──────────────────────────────────────────────────────────
  async function handleRun() {
    if (!skillText.trim()) { log("⚠️ No skill loaded", "warn"); return; }
    if (jsonError)          { log("⚠️ Fix JSON errors first", "warn"); return; }

    setLoading(true);
    setOutput("");
    setAgentLog([]);
    setActiveTab("output");

    try {
      // Phase 1: Build agentic prompt
      setPhase("building");
      log("🧠 Building agentic prompt (lazy-loading skill sections)…", "info");
      await sleep(300);

      const { systemPrompt, userPrompt } = buildAgenticPrompt(skillText, jsonInput);
      log(`📦 Skill chunked into ${skillText.split(/\n#{1,3} /).length} sections`, "info");
      log(`🔗 Relevant sections retrieved: top 6`, "info");
      await sleep(200);

      // Phase 2: Call LLM
      setPhase("calling");
      log(`🚀 Calling LLM → ${LLM_URL}`, "info");
      log(`   model: ${modelId}`, "info");

      const payload = {
        systemPrompt,
        userPrompt,
        modelId,
      };

      const res = await fetch(LLM_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      log(`✅ Response received (${res.status})`, "success");

      // Phase 3: Stream / read response
      setPhase("streaming");
      log("📡 Streaming response…", "info");

      const contentType = res.headers.get("content-type") || "";

      if (contentType.includes("text/event-stream")) {
        // SSE streaming
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop();
          for (const line of lines) {
            if (line.startsWith("data:")) {
              const d = line.slice(5).trim();
              if (d === "[DONE]") break;
              try {
                const j = JSON.parse(d);
                const delta = j?.choices?.[0]?.delta?.content
                           || j?.text
                           || j?.response
                           || "";
                if (delta) setOutput(prev => prev + delta);
              } catch { /* non-JSON SSE line */ }
            }
          }
        }
      } else {
        // Regular JSON response
        const data = await res.json();
        // Try all common response shapes
        const text = data?.response
                  || data?.text
                  || data?.content
                  || data?.choices?.[0]?.message?.content
                  || data?.choices?.[0]?.text
                  || data?.message
                  || JSON.stringify(data, null, 2);
        setOutput(text);
      }

      setPhase("done");
      log("✨ Done!", "success");

    } catch (err) {
      setPhase("error");
      log(`❌ Error: ${err.message}`, "error");
      setOutput(`Error: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // ── PHASE BADGE ───────────────────────────────────────────────────────
  const phaseMeta = {
    idle:          { label: "Ready",        color: "#6b7280" },
    "loading-skill": { label: "Loading…",  color: "#f59e0b" },
    building:      { label: "Building Prompt", color: "#8b5cf6" },
    calling:       { label: "Calling LLM", color: "#3b82f6" },
    streaming:     { label: "Streaming…",  color: "#10b981" },
    done:          { label: "Done",        color: "#10b981" },
    error:         { label: "Error",       color: "#ef4444" },
  };
  const pm = phaseMeta[phase] || phaseMeta.idle;

  // ══════════════════════════════════════════════════════════════════════
  return (
    <div style={S.root}>
      <style>{CSS}</style>

      {/* ── HEADER ── */}
      <header style={S.header}>
        <div style={S.headerLeft}>
          <span style={S.logo}>⬡</span>
          <div>
            <div style={S.headerTitle}>Agentic Skill Runner</div>
            <div style={S.headerSub}>Local LLM · {LLM_URL}</div>
          </div>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:12 }}>
          <div style={{ ...S.phaseBadge, background: pm.color + "22", color: pm.color, borderColor: pm.color + "44" }}>
            {phase === "streaming" && <span style={S.dot} />}
            {pm.label}
          </div>
          <input
            style={S.modelInput}
            value={modelId}
            onChange={e => setModelId(e.target.value)}
            placeholder="model id"
          />
        </div>
      </header>

      {/* ── 3-PANE BODY ── */}
      <div style={S.body}>

        {/* ══ PANE 1: SKILL ══ */}
        <div style={S.pane}>
          <div style={S.paneHeader}>
            <span style={S.paneIcon}>📋</span>
            <span style={S.paneTitle}>Skill / Agent</span>
            {skillName && <span style={S.skillBadge}>{skillName.slice(0,28)}</span>}
          </div>

          <div style={S.paneBody}>
            {/* drop zone */}
            <div
              style={S.dropZone}
              onClick={() => fileRef.current.click()}
              onDragOver={e => e.preventDefault()}
              onDrop={e => {
                e.preventDefault();
                const f = e.dataTransfer.files[0];
                if (f) handleFileLoad({ target: { files: [f] } });
              }}
            >
              <input ref={fileRef} type="file" accept=".md,.txt" style={{ display:"none" }} onChange={handleFileLoad} />
              <div style={S.dropIcon}>↑</div>
              <div style={S.dropText}>Drop <strong>.md</strong> skill file or click</div>
              <div style={S.dropSub}>SKILL.md · agent.md · any markdown</div>
            </div>

            <div style={S.orDivider}><span>or paste directly</span></div>

            <textarea
              style={S.textarea}
              value={skillText}
              onChange={handleSkillPaste}
              placeholder={"# My Skill\n\nYou are an expert in...\n\n## Instructions\n- Do X\n- Do Y"}
              spellCheck={false}
            />

            {skillText && (
              <div style={S.skillMeta}>
                {skillText.split("\n").length} lines · {Math.round(skillText.length / 4)} tokens est.
              </div>
            )}
          </div>
        </div>

        {/* ══ PANE 2: JSON INPUT ══ */}
        <div style={S.pane}>
          <div style={S.paneHeader}>
            <span style={S.paneIcon}>{ }</span>
            <span style={S.paneTitle}>JSON Input Data</span>
            {jsonError
              ? <span style={{ ...S.skillBadge, background:"#ef444422", color:"#ef4444" }}>⚠ invalid</span>
              : jsonInput.trim() && <span style={{ ...S.skillBadge, background:"#10b98122", color:"#10b981" }}>✓ valid</span>
            }
          </div>

          <div style={S.paneBody}>
            <textarea
              style={{ ...S.textarea, flex: 1, fontFamily:"'Fira Code', monospace", fontSize:12 }}
              value={jsonInput}
              onChange={e => handleJsonChange(e.target.value)}
              spellCheck={false}
            />
            {jsonError && <div style={S.jsonError}>{jsonError}</div>}

            {/* Quick templates */}
            <div style={S.templates}>
              <div style={S.templateLabel}>Templates →</div>
              {[
                ["Email", '{"type":"email","from":"alice@co.com","subject":"Q4 Review","body":"Hi, can we meet Tuesday?"}'],
                ["Task",  '{"task":"Summarize","priority":"high","assignee":"Raju","due":"2026-06-10"}'],
                ["Data",  '{"records":[{"id":1,"value":42},{"id":2,"value":88}],"metric":"performance"}'],
              ].map(([name, val]) => (
                <button
                  key={name}
                  style={S.tplBtn}
                  onClick={() => handleJsonChange(JSON.stringify(JSON.parse(val), null, 2))}
                >{name}</button>
              ))}
            </div>
          </div>
        </div>

        {/* ══ PANE 3: OUTPUT ══ */}
        <div style={{ ...S.pane, flex: 1.4 }}>
          <div style={S.paneHeader}>
            <span style={S.paneIcon}>✦</span>
            <span style={S.paneTitle}>LLM Output</span>
            <div style={{ marginLeft:"auto", display:"flex", gap:6 }}>
              {["output","raw","log"].map(t => (
                <button
                  key={t}
                  style={{ ...S.tabBtn, ...(activeTab===t ? S.tabActive : {}) }}
                  onClick={() => setActiveTab(t)}
                >{t}</button>
              ))}
            </div>
          </div>

          <div style={S.paneBody}>
            {/* RUN BUTTON */}
            <button
              style={{ ...S.runBtn, ...(loading ? S.runBtnLoading : {}) }}
              onClick={handleRun}
              disabled={loading}
            >
              {loading
                ? <><span style={S.spinner} /> Running…</>
                : <>▶ Run Skill</>
              }
            </button>

            {/* OUTPUT TABS */}
            <div ref={outputRef} style={S.outputBox}>
              {activeTab === "output" && (
                output
                  ? <div
                      className="md-output"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(output) }}
                    />
                  : <div style={S.outputEmpty}>
                      {loading
                        ? <div style={S.thinkingDots}><span/><span/><span/></div>
                        : <span>Output will appear here after running</span>
                      }
                    </div>
              )}

              {activeTab === "raw" && (
                <pre style={S.rawPre}>{output || "(no output yet)"}</pre>
              )}

              {activeTab === "log" && (
                <div style={S.logBox}>
                  {agentLog.length === 0 && <span style={{ color:"#555" }}>Agent log will appear here…</span>}
                  {agentLog.map((entry, i) => (
                    <div key={i} style={{ ...S.logEntry, color: logColor(entry.type) }}>
                      <span style={S.logTs}>{new Date(entry.ts).toLocaleTimeString()}</span>
                      {entry.msg}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* copy button */}
            {output && (
              <button
                style={S.copyBtn}
                onClick={() => navigator.clipboard.writeText(output)}
              >Copy</button>
            )}
          </div>
        </div>
      </div>

      {/* ── FOOTER ── */}
      <footer style={S.footer}>
        <span>Skill sections lazy-loaded · Agentic prompt assembly · Streams to UI</span>
        <span>Local LLM · No cloud · No API key</span>
      </footer>
    </div>
  );
}

function logColor(type) {
  return { info:"#8b949e", success:"#3fb950", warn:"#d29922", error:"#ef4444" }[type] || "#8b949e";
}

// ══════════════════════════════════════════════════════════════════════════
//  STYLES
// ══════════════════════════════════════════════════════════════════════════

const S = {
  root: {
    display:"flex", flexDirection:"column", height:"100vh",
    background:"#0d1117", color:"#e6edf3",
    fontFamily:"'DM Sans', 'Segoe UI', sans-serif",
    overflow:"hidden",
  },
  header: {
    display:"flex", alignItems:"center", justifyContent:"space-between",
    padding:"10px 20px", background:"#161b22",
    borderBottom:"1px solid #21262d",
    flexShrink:0,
  },
  headerLeft: { display:"flex", alignItems:"center", gap:12 },
  logo: { fontSize:24, color:"#0078d4" },
  headerTitle: { fontSize:16, fontWeight:700, color:"#e6edf3", letterSpacing:-0.3 },
  headerSub: { fontSize:11, color:"#6b7280", fontFamily:"monospace" },
  phaseBadge: {
    fontSize:11, fontWeight:600, padding:"3px 10px",
    borderRadius:20, border:"1px solid", display:"flex", alignItems:"center", gap:5,
  },
  dot: {
    width:6, height:6, borderRadius:"50%", background:"currentColor",
    animation:"pulse 1s infinite",
  },
  modelInput: {
    background:"#21262d", border:"1px solid #30363d", borderRadius:6,
    color:"#e6edf3", padding:"4px 10px", fontSize:12,
    fontFamily:"monospace", width:140,
  },
  body: {
    display:"flex", flex:1, overflow:"hidden", gap:0,
  },
  pane: {
    flex:1, display:"flex", flexDirection:"column",
    borderRight:"1px solid #21262d", overflow:"hidden",
  },
  paneHeader: {
    display:"flex", alignItems:"center", gap:8,
    padding:"10px 14px", background:"#161b22",
    borderBottom:"1px solid #21262d", flexShrink:0,
  },
  paneIcon: { fontSize:14 },
  paneTitle: { fontSize:13, fontWeight:600, color:"#e6edf3" },
  skillBadge: {
    marginLeft:"auto", fontSize:10, padding:"2px 8px",
    borderRadius:10, background:"#0078d422", color:"#58a6ff",
    fontWeight:600, maxWidth:160, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap",
  },
  paneBody: {
    flex:1, overflow:"auto", padding:12, display:"flex", flexDirection:"column", gap:10,
  },
  dropZone: {
    border:"2px dashed #30363d", borderRadius:10, padding:"20px 10px",
    textAlign:"center", cursor:"pointer", transition:"all .2s",
    background:"#0d1117",
    ":hover": { borderColor:"#0078d4" },
  },
  dropIcon: { fontSize:22, color:"#30363d", marginBottom:6 },
  dropText: { fontSize:13, color:"#8b949e" },
  dropSub: { fontSize:11, color:"#484f58", marginTop:3 },
  orDivider: {
    display:"flex", alignItems:"center", gap:8, color:"#484f58", fontSize:11,
    "::before": { content:'""', flex:1, height:1, background:"#21262d" },
    "::after":  { content:'""', flex:1, height:1, background:"#21262d" },
  },
  textarea: {
    flex:1, minHeight:120, background:"#0d1117",
    border:"1px solid #21262d", borderRadius:8,
    color:"#e6edf3", padding:10, fontSize:12,
    fontFamily:"'Fira Code', monospace", resize:"none",
    outline:"none", lineHeight:1.5,
  },
  skillMeta: { fontSize:10, color:"#484f58", textAlign:"right" },
  jsonError: {
    fontSize:11, color:"#ef4444", background:"#ef444411",
    padding:"4px 8px", borderRadius:4, fontFamily:"monospace",
  },
  templates: { display:"flex", alignItems:"center", gap:6, flexWrap:"wrap" },
  templateLabel: { fontSize:10, color:"#484f58" },
  tplBtn: {
    fontSize:10, padding:"3px 10px", borderRadius:12,
    background:"#21262d", border:"1px solid #30363d",
    color:"#8b949e", cursor:"pointer",
  },
  tabBtn: {
    fontSize:10, padding:"3px 10px", borderRadius:12,
    background:"transparent", border:"1px solid #30363d",
    color:"#8b949e", cursor:"pointer", textTransform:"uppercase", letterSpacing:0.5,
  },
  tabActive: {
    background:"#0078d422", borderColor:"#0078d4", color:"#58a6ff",
  },
  runBtn: {
    width:"100%", padding:"10px", borderRadius:8,
    background:"#0078d4", border:"none", color:"white",
    fontSize:14, fontWeight:700, cursor:"pointer",
    display:"flex", alignItems:"center", justifyContent:"center", gap:8,
    transition:"all .15s", flexShrink:0,
  },
  runBtnLoading: { background:"#1f2937", color:"#6b7280", cursor:"not-allowed" },
  spinner: {
    width:14, height:14, border:"2px solid #6b7280",
    borderTop:"2px solid #58a6ff", borderRadius:"50%",
    animation:"spin 0.7s linear infinite", display:"inline-block",
  },
  outputBox: {
    flex:1, overflow:"auto", background:"#0d1117",
    border:"1px solid #21262d", borderRadius:8, padding:14,
  },
  outputEmpty: {
    height:"100%", display:"flex", alignItems:"center", justifyContent:"center",
    color:"#484f58", fontSize:13,
  },
  thinkingDots: { display:"flex", gap:6 },
  rawPre: {
    fontFamily:"'Fira Code', monospace", fontSize:11,
    color:"#8b949e", whiteSpace:"pre-wrap", lineHeight:1.6, margin:0,
  },
  logBox: { fontFamily:"monospace", fontSize:11, lineHeight:2, display:"flex", flexDirection:"column", gap:2 },
  logEntry: { display:"flex", gap:10, alignItems:"baseline" },
  logTs: { color:"#484f58", fontSize:10, flexShrink:0 },
  copyBtn: {
    alignSelf:"flex-end", fontSize:11, padding:"4px 12px",
    background:"#21262d", border:"1px solid #30363d",
    color:"#8b949e", borderRadius:6, cursor:"pointer", flexShrink:0,
  },
  footer: {
    display:"flex", justifyContent:"space-between", padding:"6px 20px",
    background:"#0a0d12", borderTop:"1px solid #21262d",
    fontSize:10, color:"#484f58", flexShrink:0,
  },
};

const CSS = `
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700&family=Fira+Code:wght@400;500&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }

@keyframes spin  { to { transform: rotate(360deg); } }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }
@keyframes fadeIn { from { opacity:0; transform:translateY(4px); } to { opacity:1; transform:none; } }

.md-output { animation: fadeIn .3s ease; line-height: 1.7; }
.md-h1 { font-size:1.3em; font-weight:700; color:#e6edf3; margin:16px 0 8px; border-bottom:1px solid #21262d; padding-bottom:6px; }
.md-h2 { font-size:1.1em; font-weight:700; color:#c9d1d9; margin:14px 0 6px; }
.md-h3 { font-size:1em;   font-weight:600; color:#8b949e; margin:10px 0 4px; }
.md-p  { color:#c9d1d9; margin:6px 0; font-size:13px; }
.md-ul { padding-left:18px; margin:6px 0; }
.md-li { color:#c9d1d9; font-size:13px; margin:3px 0; }
.md-code { background:#161b22; color:#79c0ff; padding:1px 5px; border-radius:4px; font-size:11px; font-family:'Fira Code',monospace; }
.md-bq { border-left:3px solid #0078d4; padding:6px 12px; background:#0078d411; color:#8b949e; font-size:13px; border-radius:0 6px 6px 0; margin:8px 0; }
.md-h3:contains("KEY INSIGHTS"), .md-h2:contains("KEY INSIGHTS") { color:#58a6ff; }

textarea:focus { border-color: #0078d4 !important; outline: none !important; }
button:hover:not(:disabled) { opacity: 0.85; }
`;
