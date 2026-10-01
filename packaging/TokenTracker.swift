// Token Tracker — native macOS shell around token_meter.py.
// Runs the Python backend, shows the dashboard in a small window, and keeps a
// live "% left" readout in the menu bar even when the window is closed.
import AppKit
import ServiceManagement
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKUIDelegate, NSMenuDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var statusItem: NSStatusItem!
    var backend: Process?
    var port: Int?
    var quitting = false
    let onTopItem = NSMenuItem(title: "Keep Window on Top", action: #selector(toggleOnTop), keyEquivalent: "t")
    let loginItem = NSMenuItem(title: "Open at Login", action: #selector(toggleLogin), keyEquivalent: "")

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMainMenu()
        buildStatusItem()
        buildWindow()
        startBackend()
    }

    // MARK: backend

    func pythonPath() -> String {
        for p in ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: p) { return p }
        return "/usr/bin/python3"
    }

    func startBackend() {
        guard let script = Bundle.main.path(forResource: "token_meter", ofType: "py") else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonPath())
        p.arguments = [script, "--port", "0", "--no-window",
                       "--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        out.fileHandleForReading.readabilityHandler = { [weak self] h in
            guard let s = String(data: h.availableData, encoding: .utf8) else { return }
            for line in s.split(separator: "\n") where line.hasPrefix("READY ") {
                if let port = Int(line.dropFirst(6)) {
                    DispatchQueue.main.async { self?.backendReady(port) }
                }
            }
        }
        p.terminationHandler = { [weak self] _ in
            // restart the backend if it ever dies
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                guard let self = self, !self.quitting else { return }
                self.startBackend()
            }
        }
        do { try p.run(); backend = p } catch {
            showError("Couldn't start Python (\(pythonPath())). Install Python 3 or the Xcode Command Line Tools.")
        }
    }

    func backendReady(_ port: Int) {
        self.port = port
        webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        backend?.terminate()
    }

    // MARK: window

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "status")
        config.userContentController.add(self, name: "theme")
        // leave room for the traffic-light buttons under the transparent title bar
        config.userContentController.addUserScript(WKUserScript(
            source: "document.documentElement.classList.add('mac-app')",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 780),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Token Tracker"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = NSColor(red: 0xfa/255, green: 0xf9/255, blue: 0xf5/255, alpha: 1)
        window.minSize = NSSize(width: 360, height: 480)
        window.isReleasedWhenClosed = false
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("TokenTrackerWindow")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // Closing the window keeps tracking in the menu bar; reopen from Dock or menu bar.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        showWindow(); return true
    }

    @objc func toggleOnTop() {
        window.level = window.level == .floating ? .normal : .floating
        onTopItem.state = window.level == .floating ? .on : .off
    }

    @objc func toggleLogin() {
        let svc = SMAppService.mainApp
        do {
            if svc.status == .enabled { try svc.unregister() } else { try svc.register() }
        } catch {
            showError("Couldn't change login item: \(error.localizedDescription)\nTip: move Token Tracker to Applications first.")
        }
        loginItem.state = svc.status == .enabled ? .on : .off
    }

    func menuWillOpen(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    // MARK: menu bar

    func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let b = statusItem.button {
            b.image = NSImage(systemSymbolName: "gauge.with.dots.needle.67percent", accessibilityDescription: "Token Tracker")
            b.imagePosition = .imageLeading
            b.title = " –"
        }
        let menu = NSMenu()
        menu.delegate = self
        menu.addItem(withTitle: "Show Token Tracker", action: #selector(showWindow), keyEquivalent: "")
        menu.addItem(onTopItem)
        menu.addItem(loginItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Token Tracker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        statusItem.menu = menu
    }

    // Dashboard posts "CC 62% · CX 55%" on every update, and its background color on tab change.
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let text = message.body as? String else { return }
        if message.name == "status" {
            statusItem.button?.title = " " + text
        } else if message.name == "theme", let color = NSColor(hex: text) {
            window.backgroundColor = color
            // light titlebar buttons on dark themes, dark on light ones
            let rgb = color.usingColorSpace(.sRGB) ?? color
            let luma = 0.299 * rgb.redComponent + 0.587 * rgb.greenComponent + 0.114 * rgb.blueComponent
            window.appearance = NSAppearance(named: luma < 0.5 ? .darkAqua : .aqua)
        }
    }

    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Token Tracker", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Token Tracker", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit Token Tracker", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "Window")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r")
        winItem.submenu = winMenu
        NSApp.mainMenu = main
    }

    @objc func reload() { webView.reload() }

    // MARK: JS prompt() → native dialog (used to set the 5-hour limit)

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = "5-hour token limit"
        alert.informativeText = prompt
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        completionHandler(alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    func showError(_ text: String) {
        let a = NSAlert(); a.messageText = "Token Tracker"; a.informativeText = text; a.runModal()
    }
}

extension NSColor {
    convenience init?(hex: String) {
        var h = hex.trimmingCharacters(in: .whitespaces)
        guard h.hasPrefix("#") else { return nil }
        h.removeFirst()
        guard h.count == 6, let v = UInt32(h, radix: 16) else { return nil }
        self.init(srgbRed: CGFloat(v >> 16 & 0xff) / 255, green: CGFloat(v >> 8 & 0xff) / 255,
                  blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
