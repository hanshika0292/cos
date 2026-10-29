// cos window snapshot: browser tabs, iTerm tabs, other app windows, and what's in front.
// Run with `osascript -l JavaScript snap.js`. Never launches an app; each query is one bulk Apple Event.
function run() {
  const out = [];
  const push = (k, app, title, url, front) => { if (title || url) out.push({ k, app, title: title || "", url: url || "", front: !!front }); };
  const se = Application("System Events");

  let frontApp = "";
  try { frontApp = se.processes.whose({ frontmost: true }).name()[0] || ""; } catch (e) {}

  const browsers = ["Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge"];
  for (const name of browsers) {
    let a;
    try { a = Application(name); if (!a.running()) continue; } catch (e) { continue; }
    try {
      const chromium = name !== "Safari";
      const titles = a.windows.tabs.name();
      const urls = a.windows.tabs.url();
      const modes = chromium ? a.windows.mode() : [];
      let active = [];
      try { active = chromium ? a.windows.activeTab.url() : a.windows.currentTab.url(); } catch (e) {}
      titles.forEach((ts, w) => {
        if (modes[w] === "incognito") return;              // private windows are never recorded
        (ts || []).forEach((t, i) => {
          const u = (urls[w] || [])[i];
          push("tab", name, t, u, name === frontApp && w === 0 && u === active[0]);
        });
      });
    } catch (e) {}
  }

  try {
    const it = Application("iTerm2");
    if (it.running()) {
      it.windows.tabs.currentSession.name().forEach((ts, w) =>
        (ts || []).forEach(t => push("term", "iTerm2", t, "", frontApp === "iTerm2" && w === 0)));
    }
  } catch (e) {}

  // Other apps' window titles (Numbers, Keynote, Excel, Figma, Slack...). Needs Accessibility; silently skipped without it.
  const skip = browsers.concat(["iTerm2", "Orca", "Finder", "SwiftBar", "System Events", "loginwindow"]);
  try {
    const procs = se.processes.whose({ backgroundOnly: false });
    const names = procs.name();
    const wins = procs.windows.name();
    names.forEach((n, i) => {
      if (skip.indexOf(n) >= 0) return;
      (wins[i] || []).forEach((t, j) => push("win", n, t, "", n === frontApp && j === 0));
    });
  } catch (e) {}

  if (frontApp && !out.some(x => x.front)) push("win", frontApp, frontApp, "", true);
  return JSON.stringify(out);
}
