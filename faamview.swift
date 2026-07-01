// FAAM native window — a WKWebView app that hosts the local FAAM server.
// Compiled by the launcher into the app bundle so FAAM opens as a real macOS
// application window (no browser). Closing the window stops the server.
import Cocoa
import WebKit
import Darwin

let resourcesDir: String = {
    if CommandLine.arguments.count > 1 { return CommandLine.arguments[1] }
    let exe = CommandLine.arguments[0] as NSString
    let macos = exe.deletingLastPathComponent as NSString          // .../Contents/MacOS
    return macos.deletingLastPathComponent + "/Resources"          // .../Contents/Resources
}()
let faamDir = NSHomeDirectory() + "/.faam"
let port = Int(ProcessInfo.processInfo.environment["FAAM_PORT"] ?? "") ?? 8765
try? FileManager.default.createDirectory(atPath: faamDir, withIntermediateDirectories: true)

// ── Distribution mode ───────────────────────────────────────────────────────
// Set bakedRemoteURL to your deployed FAAM (e.g. "https://app.yourdomain.com")
// to ship a THIN CLIENT: the window talks to your hosted backend, so no local
// server runs and no API key ever touches the user's machine. Leave it empty to
// run the bundled Python server locally (development / self-host).
let bakedRemoteURL = "https://faam.onrender.com"
let remoteURL: String = {
    let raw = ProcessInfo.processInfo.environment["FAAM_REMOTE_URL"].flatMap { $0.isEmpty ? nil : $0 } ?? bakedRemoteURL
    return raw.trimmingCharacters(in: CharacterSet(charactersIn: "/ \n\t"))
}()
let isRemote = !remoteURL.isEmpty

// App Store builds must be REMOTE-ONLY: the bundled Python server is NEVER run
// (a local server + interpreter isn't allowed under the Mac App Store sandbox).
// Flip to `true` (or launch with FAAM_REMOTE_ONLY=1) for the App Store build;
// leave false for local/self-host dev, which keeps the Python fallback.
let remoteOnly = (ProcessInfo.processInfo.environment["FAAM_REMOTE_ONLY"] == "1")

func diskKey() -> String? {
    if let e = ProcessInfo.processInfo.environment["OPENAI_API_KEY"], !e.isEmpty { return e }
    if let s = try? String(contentsOfFile: faamDir + "/key", encoding: .utf8) {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { return t }
    }
    return nil
}

func killOldServer() {
    if let s = try? String(contentsOfFile: faamDir + "/server.pid", encoding: .utf8),
       let pid = Int32(s.trimmingCharacters(in: .whitespacesAndNewlines)), pid > 1 {
        kill(pid, SIGTERM)
        Thread.sleep(forTimeInterval: 0.4)
    }
}

func launchServer(key: String) -> Process {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    p.arguments = ["python3", resourcesDir + "/app.py"]
    var env = ProcessInfo.processInfo.environment
    env["FAAM_PORT"] = String(port)
    env["OPENAI_API_KEY"] = key
    p.environment = env
    p.currentDirectoryURL = URL(fileURLWithPath: resourcesDir)
    try? p.run()
    try? String(p.processIdentifier).write(toFile: faamDir + "/server.pid", atomically: true, encoding: .utf8)
    return p
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKUIDelegate, WKScriptMessageHandler {
    var server: Process?
    var window: NSWindow?
    var webView: WKWebView?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.activate(ignoringOtherApps: true)
        buildMenu()
        buildWindow()

        // Thin-client mode: talk to the hosted backend (no local server, no key).
        // If the backend is unreachable, fall back to running locally so the app
        // never lands on a dead page.
        if isRemote {
            probeRemote { [weak self] ok in
                guard let self = self else { return }
                if ok, let u = URL(string: remoteURL + "/login") {
                    self.webView?.load(URLRequest(url: u))
                } else if remoteOnly {
                    self.showRemoteError()          // App Store: never fall back to Python
                } else {
                    self.startLocal()
                }
            }
            return
        }
        if remoteOnly { showRemoteError(); return }  // safety: never run Python here either
        startLocal()
    }

    // Shown only in remote-only (App Store) builds when the backend is offline.
    func showRemoteError() {
        let html = """
        <html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
        <style>body{font-family:-apple-system,system-ui;background:#0B1220;color:#E7ECF3;
        display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center;text-align:center}
        .b{max-width:360px;padding:24px}h1{font-size:20px;margin:0 0 8px}p{color:#8A97AD;line-height:1.5}
        button{margin-top:16px;padding:10px 20px;border:0;border-radius:10px;background:#2E64F0;color:#fff;font-weight:700;cursor:pointer}</style>
        </head><body><div class='b'><h1>Can’t reach FAAM</h1>
        <p>FAAM couldn’t connect to its server. Check your internet connection and try again.</p>
        <button onclick='location.reload()'>Retry</button></div></body></html>
        """
        webView?.loadHTMLString(html, baseURL: URL(string: remoteURL))
    }

    // Is the hosted backend up? (Short timeout so startup never stalls.)
    func probeRemote(_ done: @escaping (Bool) -> Void) {
        guard let u = URL(string: remoteURL + "/api/health") else { done(false); return }
        var req = URLRequest(url: u); req.timeoutInterval = 4
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async { done((resp as? HTTPURLResponse)?.statusCode == 200) }
        }.resume()
    }

    // Run the bundled Python server with the user's own key (self-host fallback).
    func startLocal() {
        var key = diskKey()
        if key == nil { key = promptForKey() }
        guard let k = key, !k.isEmpty else { NSApp.terminate(nil); return }
        try? k.write(toFile: faamDir + "/key", atomically: true, encoding: .utf8)
        killOldServer()
        server = launchServer(key: k)
        poll(0)
    }

    func promptForKey() -> String? {
        let a = NSAlert()
        a.messageText = "Welcome to FAAM"
        a.informativeText = "Paste your OpenAI API key (sk-...).\nStored only on this Mac at ~/.faam/key."
        a.addButton(withTitle: "Start")
        a.addButton(withTitle: "Quit")
        let f = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        a.accessoryView = f
        return a.runModal() == .alertFirstButtonReturn
            ? f.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) : nil
    }

    func buildWindow() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 880),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "FAAM"
        w.minSize = NSSize(width: 900, height: 600)
        w.center()
        let cfg = WKWebViewConfiguration()
        // Bridge so the page can open broker / Stripe / news links in the real browser.
        cfg.userContentController.add(self, name: "faamOpen")
        let wv = WKWebView(frame: w.contentView!.bounds, configuration: cfg)
        wv.autoresizingMask = [.width, .height]
        wv.uiDelegate = self
        w.contentView!.addSubview(wv)
        w.delegate = self
        w.makeKeyAndOrderFront(nil)
        window = w
        webView = wv
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit FAAM", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        NSApp.mainMenu = main
    }

    func poll(_ attempt: Int) {
        let dash = URL(string: "http://localhost:\(port)/login")!
        if attempt > 50 { webView?.load(URLRequest(url: dash)); return }
        var req = URLRequest(url: URL(string: "http://localhost:\(port)/api/health")!)
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 {
                    self?.webView?.load(URLRequest(url: dash))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { self?.poll(attempt + 1) }
                }
            }
        }.resume()
    }

    // Open target="_blank" / window.open links (e.g. news headlines, broker pages)
    // in the user's default browser instead of a dead in-app window.
    func webView(_ webView: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // Page bridge: window.webkit.messageHandlers.faamOpen.postMessage(url)
    // Reliably opens broker / Stripe checkout / news links in the default browser.
    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "faamOpen", let s = message.body as? String,
           let url = URL(string: s.trimmingCharacters(in: .whitespacesAndNewlines)) {
            NSWorkspace.shared.open(url)
        }
    }

    // Grant microphone access so voice mode works inside the app (macOS 12+).
    @available(macOS 12.0, *)
    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }

    func windowWillClose(_ note: Notification) { NSApp.terminate(nil) }
    func applicationWillTerminate(_ note: Notification) { server?.terminate() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
