const { ModelDerivativeClient, ManifestHelper } = require("forge-server-utils");
const { SvfReader, GltfWriter } = require("forge-convert-utils");
const path = require("path");
const fs = require("fs");
const https = require("https");

// Load .env manually
const envPath = path.resolve(__dirname, "../../.env");
if (fs.existsSync(envPath)) {
  const lines = fs.readFileSync(envPath, "utf-8").split("\n");
  for (const line of lines) {
    const match = line.match(/^([^#=]+)=(.*)$/);
    if (match) process.env[match[1].trim()] = match[2].trim();
  }
}

const APS_CLIENT_ID = process.env.APS_CLIENT_ID;
const APS_CLIENT_SECRET = process.env.APS_CLIENT_SECRET;

async function getToken() {
  const creds = Buffer.from(`${APS_CLIENT_ID}:${APS_CLIENT_SECRET}`).toString("base64");
  const body = "grant_type=client_credentials&scope=data:read data:write data:create bucket:create bucket:read viewables:read";

  return new Promise((resolve, reject) => {
    const req = https.request(
      "https://developer.api.autodesk.com/authentication/v2/token",
      {
        method: "POST",
        headers: {
          Authorization: `Basic ${creds}`,
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => {
          if (res.statusCode !== 200) return reject(new Error(`Auth failed: ${res.statusCode} ${data}`));
          resolve(JSON.parse(data).access_token);
        });
      }
    );
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

async function run() {
  const urn = process.argv[2];
  const outputDir = process.argv[3] || path.resolve(__dirname, "../../output_glb_aps");

  if (!urn) {
    console.error("Usage: node svf_to_glb.js <urn> [output_dir]");
    process.exit(1);
  }

  if (!APS_CLIENT_ID || !APS_CLIENT_SECRET) {
    console.error("Set APS_CLIENT_ID and APS_CLIENT_SECRET env vars");
    process.exit(1);
  }

  fs.mkdirSync(outputDir, { recursive: true });

  console.log("[svf-glb] authenticating via v2...");
  const token = await getToken();
  console.log("[svf-glb] got token");

  const auth = { token };
  const modelDerivativeClient = new ModelDerivativeClient(auth);

  console.log("[svf-glb] fetching manifest...");
  const manifest = await modelDerivativeClient.getManifest(urn);
  const helper = new ManifestHelper(manifest);
  const derivatives = helper.search({ type: "resource", role: "graphics" });

  console.log(`[svf-glb] found ${derivatives.length} graphics resources`);
  for (const d of derivatives) {
    console.log(`  - ${d.mime} guid=${d.guid}`);
  }

  let converted = 0;
  for (const derivative of derivatives) {
    if (derivative.mime !== "application/autodesk-svf") {
      console.log(`[svf-glb] skipping ${derivative.mime}`);
      continue;
    }

    console.log(`[svf-glb] reading SVF: ${derivative.guid}`);
    const reader = await SvfReader.FromDerivativeService(urn, derivative.guid, auth);
    const scene = await reader.read({ log: console.log });

    const writer = new GltfWriter({
      deduplicate: true,
      skipUnusedUvs: true,
      center: true,
    });
    await writer.write(scene, outputDir);
    converted++;
    console.log(`[svf-glb] wrote output to ${outputDir}`);
  }

  if (converted === 0) {
    console.error("[svf-glb] no SVF derivatives found to convert");
    process.exit(1);
  }

  // List output files
  const files = fs.readdirSync(outputDir);
  console.log(`\n[svf-glb] output files:`);
  for (const f of files) {
    const stat = fs.statSync(path.join(outputDir, f));
    console.log(`  ${f} (${(stat.size / 1024).toFixed(1)} KB)`);
  }
}

run().catch((err) => {
  console.error("[svf-glb] error:", err.message || err);
  if (err.stack) console.error(err.stack);
  process.exit(1);
});
