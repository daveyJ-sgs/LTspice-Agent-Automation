import AppKit

// A native launcher for an existing source installation, not a bundled Python runtime.
final class Launcher: NSObject, NSApplicationDelegate {
    private let server = Process()
    private var logURL: URL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        let item = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Open System Builder", action: #selector(openGUI), keyEquivalent: "o")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit System Builder", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = appMenu
        menu.addItem(item)
        NSApp.mainMenu = menu

        do {
            let root = Bundle.main.object(forInfoDictionaryKey: "LSBRepositoryPath") as! String
            let python = root + "/.venv/bin/python"
            guard FileManager.default.isExecutableFile(atPath: python) else {
                throw NSError(domain: "LSB", code: 1, userInfo: [NSLocalizedDescriptionKey:
                    "Run Start-SystemBuilder.command in the repository once to install the Python environment."])
            }
            let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/LTspice System Builder")
            try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
            logURL = logs.appendingPathComponent("launcher.log")
            try Data().write(to: logURL)
            let log = try FileHandle(forWritingTo: logURL)
            server.executableURL = URL(fileURLWithPath: python)
            server.arguments = [root + "/system_builder.py", "--workspace",
                FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Documents/LTspice/projects").path]
            server.currentDirectoryURL = URL(fileURLWithPath: root)
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            environment["PYTHONUNBUFFERED"] = "1"
            server.environment = environment
            server.standardOutput = log
            server.standardError = log
            server.terminationHandler = { process in
                DispatchQueue.main.async {
                    if process.terminationStatus != 0 {
                        self.showError("System Builder stopped. See " + self.logURL.path)
                    }
                    NSApp.terminate(nil)
                }
            }
            try server.run()
        } catch {
            showError(error.localizedDescription)
            NSApp.terminate(nil)
        }
    }

    @objc func openGUI() {
        guard let logURL, let text = try? String(contentsOf: logURL, encoding: .utf8),
              let line = text.components(separatedBy: "\n").first(where: { $0.hasPrefix("LTspice System Builder: http://127.0.0.1:") }),
              let url = URL(string: String(line.dropFirst("LTspice System Builder: ".count))) else { return }
        NSWorkspace.shared.open(url)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openGUI()
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.terminationHandler = nil
        if server.isRunning { server.interrupt() }
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "LTspice System Builder"
        alert.informativeText = message
        alert.runModal()
    }
}

let app = NSApplication.shared
let launcher = Launcher()
app.delegate = launcher
app.setActivationPolicy(.regular)
app.run()
