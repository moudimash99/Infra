// Uptime Kuma (status.machaka.net) from the command line. Its checks live in its own database, not in this repo,
// so this script is the written-down way to add one. It runs inside the Kuma pod, which has socket.io-client:
//
//   P=$(kubectl -n tools get secret tool-logins -o jsonpath='{.data.kuma}' | base64 -d)
//   kubectl -n tools exec -i deploy/uptime-kuma -- sh -c 'cat > /tmp/kuma.js' < scripts/kuma.js
//   kubectl -n tools exec deploy/uptime-kuma -- env KUMA_PASS="$P" node /tmp/kuma.js list
//   kubectl -n tools exec deploy/uptime-kuma -- env KUMA_PASS="$P" node /tmp/kuma.js add-site "Spirit Island" https://spirit.machaka.net
//   kubectl -n tools exec deploy/uptime-kuma -- rm /tmp/kuma.js
// (In Git Bash on Windows, put MSYS_NO_PATHCONV=1 in front of the kubectl lines, or /tmp turns into a Windows path.)
//
// add-site <name> <url> [group]: an HTTP check with the same settings as the site that heads the status page's
// "Sites" group (every minute, down after 3 failures, the "Email (Gmail)" notification), then puts it at the end of
// that group (default "Sites") on the status page `homelab`, which feeds the home page's Uptime Kuma tile.
// Does nothing if a check of that URL already exists. list: every check and the status page's groups.
const { io } = require("/app/node_modules/socket.io-client");

const SLUG = "homelab";
const BASE = "http://localhost:3001";
// Settings copied from the template check (the rest are Kuma's defaults for a new HTTP check, as its form sends them).
const COPY = ["interval", "retryInterval", "resendInterval", "maxretries", "timeout", "ignoreTls", "upsideDown",
  "maxredirects", "accepted_statuscodes", "expiryNotification", "method"];

const [cmd, ...args] = process.argv.slice(2);
const s = io(BASE, { transports: ["websocket"], reconnection: false });
const call = (ev, ...a) => new Promise((ok) => s.emit(ev, ...a, ok));
const quit = (code, msg) => { if (msg) console.log(msg); s.close(); process.exit(code); };
setTimeout(() => quit(2, "timed out (Kuma did not answer in 30 s)"), 30000);

let monitors = null;
s.on("monitorList", (list) => { monitors = Object.values(list); });
s.on("connect_error", (e) => quit(1, `cannot reach Kuma: ${e.message}`));

s.on("connect", async () => {
  const r = await call("login", { username: "admin", password: process.env.KUMA_PASS, token: "" });
  if (!r.ok) quit(1, `login failed: ${r.msg}`);
  while (!monitors) await new Promise((ok) => setTimeout(ok, 200));
  const page = await fetch(`${BASE}/api/status-page/${SLUG}`).then((x) => x.json());
  const groups = page.publicGroupList;

  if (cmd === "list") {
    for (const m of monitors) console.log(`${m.id}\t${m.active ? "on " : "off"}\t${m.type}\t${m.name}\t${m.url || m.hostname || ""}`);
    for (const g of groups) console.log(`status page group "${g.name}": ${g.monitorList.map((m) => m.name).join(", ")}`);
    quit(0);
  }

  if (cmd === "add-site") {
    const [name, url, groupName = "Sites"] = args;
    if (!name || !/^https?:\/\//.test(url || "")) quit(1, "usage: add-site <name> <url> [group]");
    const group = groups.find((g) => g.name === groupName);
    if (!group) quit(1, `no group "${groupName}" on status page ${SLUG}`);
    let m = monitors.find((x) => x.url === url);
    if (m) {
      console.log(`check ${m.id} "${m.name}" already watches ${url}`);
    } else {
      const tpl = monitors.find((x) => x.id === group.monitorList[0].id);
      const monitor = {
        type: "http", name, url, active: true, parent: null, notificationIDList: tpl.notificationIDList,
        dns_resolve_type: "A", dns_resolve_server: "1.1.1.1", httpBodyEncoding: "json", kafkaProducerBrokers: [],
        kafkaProducerSaslOptions: { mechanism: "None" }, rabbitmqNodes: [], conditions: [],
      };
      for (const k of COPY) monitor[k] = tpl[k];
      const res = await call("add", monitor);
      if (!res.ok) quit(1, `add failed: ${res.msg}`);
      m = { id: res.monitorID, name };
      console.log(`added check ${m.id} "${name}" (${url}), settings from "${tpl.name}"`);
    }
    if (group.monitorList.some((x) => x.id === m.id)) quit(0, `already in group "${groupName}"`);
    // saveStatusPage replaces every group, so send them all back, with the new check at the end of its group.
    const { ok, config } = await call("getStatusPage", SLUG);
    if (!ok) quit(1, "could not read the status page");
    const list = groups.map((g) => ({
      id: g.id, name: g.name,
      monitorList: [...g.monitorList, ...(g === group ? [{ id: m.id }] : [])].map((x) => ({ id: x.id, ...(x.sendUrl !== undefined && { sendUrl: x.sendUrl }) })),
    }));
    const saved = await call("saveStatusPage", SLUG, config, config.icon, list);
    if (!saved.ok) quit(1, `status page not saved: ${saved.msg}`);
    quit(0, `status page ${SLUG}: "${groupName}" now ${list.find((g) => g.id === group.id).monitorList.length} checks`);
  }

  quit(1, "commands: list | add-site <name> <url> [group]");
});
