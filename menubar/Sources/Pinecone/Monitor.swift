import Foundation
import Security
import ServiceManagement
import SwiftUI

struct Resource: Decodable, Identifiable {
    var id: String { name }
    var name = ""
    let title: String
    let kind: String
    let state: String
    let memory_gb: Double
    let whole: Bool?
    let active: Int
    let actual_gb: Double?
    let idle_s: Int?
    let draining: Bool
    let error: String?
    let keep_alive_s: Double?
    let idle_stop_s: Double?
    enum CodingKeys: String, CodingKey { case title, kind, state, memory_gb, whole, active, actual_gb, idle_s, draining, error, keep_alive_s, idle_stop_s }
}

struct Waiting: Decodable, Hashable { let resource: String; let waiting_s: Int; let reason: String? }
struct Event: Decodable, Hashable { let ago_s: Int; let text: String }
struct Host: Decodable {
    let ram_total_gb: Double
    let cpu_pct: [Double?]
    let gpu_pct: [Double?]
    let ram_used_gb: [Double?]
}

struct ConfigInfo: Decodable { let error: String?; let path: String? }
struct GpuLimit: Decodable { let managed: Bool; let wanted_mb: Double?; let error: String?; let installer: String? }
/// An address clients use: the models, or a service that told the scheduler about itself.
struct Endpoint: Decodable, Hashable { let name: String; let url: String; let tools: [String]?; let tool_errors: [String: String]? }

func plain(_ d: Double) -> String { d == d.rounded() ? String(Int(d)) : String(d) }
func number(_ s: String) -> Double? { Double(s.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")) }

struct TokenInfo: Decodable, Identifiable { let id: String; let name: String; let last_used_ago_s: Int? }
struct TokenList: Decodable { let tokens: [TokenInfo] }

struct Status: Decodable {
    let admin: Bool?
    let config: ConfigInfo?
    let system_reserve_gb: Double?
    let gpu_limit: GpuLimit?
    let budget_gb: Double
    let in_memory_gb: Double
    let resources: [String: Resource]
    let queue: [Waiting]
    let events: [Event]
    let host: Host?
    let endpoints: [Endpoint]?
}

/// An access token pasted for a scheduler on another Mac lives in the login keychain, never in the preferences file.
enum Keychain {
    static let service = "Pinecone"
    static func read() -> String {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return "" }
        return String(data: d, encoding: .utf8) ?? ""
    }
    static func write(_ value: String) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service]
        SecItemDelete(q as CFDictionary)
        guard !value.isEmpty else { return }
        var add = q
        add[kSecValueData as String] = Data(value.utf8)
        SecItemAdd(add as CFDictionary, nil)
    }
}

@MainActor
final class Monitor: ObservableObject {
    @AppStorage("serverURL") var serverURL = "http://127.0.0.1:8080"
    /// The launchd services the switch in the header starts and stops: the ones launchd/install.sh sets up.
    @AppStorage("serviceLabels") var serviceLabels = "dev.pinecone.scheduler, dev.pinecone.media-mcp"

    @Published var status: Status?
    @Published var online = false
    @Published var problem = ""
    @Published var serviceBusy = false
    // View state lives here so the app builds with the Command Line Tools alone (no @State macro).
    @Published var showSettings = false
    @Published var moreEvents = false
    @Published var newKey = ""
    @Published var atLogin = SMAppService.mainApp.status == .enabled
    @Published var note = ""
    // Scheduler settings being edited; they are written to the scheduler's config.json on Save.
    @Published var form: [String: String] = [:]   // every setting as text, by its place in the config (see key)
    @Published var showAddress = false            // the field for watching a scheduler on another Mac
    private var original: [String: String] = [:]
    private var kinds: [String: Kind] = [:]
    @Published var saveNote = ""
    @Published var saveFailed = false
    @Published var saving = false
    @Published var allowing = false
    @Published var allowNote = ""
    // Access tokens for other devices and apps (made and revoked by the scheduler).
    @Published var tokens: [TokenInfo] = []
    @Published var tokenName = ""
    @Published var minted = ""
    @Published var mintedName = ""
    @Published var copied = false
    @Published var tokenNote = ""
    @Published var unauthorized = false
    @Published var copiedURL = ""
    /// The key of a scheduler on this Mac, found by asking it where its key file is.
    private var found: (url: String, key: String)?
    var snapshot = false

    private var timer: Timer?
    private var keychainKey = Keychain.read()

    init(poll: Bool = true) {
        guard poll else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        }
        Task { await refresh() }
    }

    /// The scheduler's address without the scheme, for the header.
    var hostLabel: String {
        guard let u = URL(string: serverURL), let h = u.host else { return serverURL }
        return u.port.map { "\(h):\($0)" } ?? h
    }

    /// The scheduler's config file, when the scheduler runs on this Mac (it says where its file is).
    var configPath: String? {
        guard isLocal, let p = status?.config?.path, FileManager.default.fileExists(atPath: p) else { return nil }
        return p
    }

    private var base: String { serverURL.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) }
    /// True when the scheduler address is this Mac. Only then does the app read files the scheduler points to.
    var isLocal: Bool { ["127.0.0.1", "localhost", "::1"].contains(URL(string: base)?.host ?? "") }

    var isAdmin: Bool { status?.admin ?? true }

    /// A scheduler on this Mac tells local callers where its key file is, so nobody has to type a key.
    private func findKey() async {
        guard isLocal, let url = URL(string: base + "/health"),
              let (data, _) = try? await URLSession.shared.data(for: URLRequest(url: url, timeoutInterval: 3)),
              let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let file = j["key_file"] as? String,
              let s = try? String(contentsOfFile: file, encoding: .utf8) else { return }
        found = (serverURL, s.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    func openConfig() {
        if let p = configPath { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
    }

    var key: String {
        if let f = found, f.url == serverURL { return f.key }
        return keychainKey
    }

    func setKey(_ value: String) {
        keychainKey = value
        Keychain.write(value)
    }

    var hasKeychainKey: Bool { !keychainKey.isEmpty }
    var working: Bool { status?.resources.values.contains { $0.active > 0 || $0.state == "starting" || $0.state == "stopping" } ?? false }
    var resources: [Resource] {
        (status?.resources ?? [:]).map { var r = $0.value; r.name = $0.key; return r }
            .sorted { ($0.kind, $0.name) < ($1.kind, $1.name) }
    }
    var models: [Resource] { resources.filter { $0.kind == "llm" } }
    var services: [Resource] { resources.filter { $0.kind != "llm" } }

    func copy(_ url: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(url, forType: .string)
        copiedURL = url
    }
    var labels: [String] { serviceLabels.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } }

    private func request(_ path: String, body: [String: Any]? = nil) -> URLRequest? {
        guard let url = URL(string: base + path) else { return nil }
        var r = URLRequest(url: url, timeoutInterval: 4)
        r.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        if let body {
            r.httpMethod = "POST"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        return r
    }

    func refresh() async {
        if found?.url != serverURL { await findKey() }  // again each time, until the scheduler is up and answers
        guard let req = request("/scheduler/status") else { online = false; problem = "The server address is not a valid URL."; return }
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            unauthorized = code == 401
            if code == 401 { online = false; status = nil; problem = "This scheduler wants an access token. Paste one in Settings."; return }
            guard code == 200 else { online = false; problem = "The server answered with status \(code)."; return }
            status = try JSONDecoder().decode(Status.self, from: data)
            online = true
            problem = ""
        } catch is DecodingError {
            online = false
            problem = "The server answered, but not like a scheduler."
        } catch {
            online = false
            status = nil
            problem = "The scheduler is not running, or not reachable at this address."
        }
    }

    var ramTotal: Double { status?.host?.ram_total_gb ?? status?.budget_gb ?? 0 }
    var savedReserve: Double { status.map { $0.system_reserve_gb ?? max(0, ramTotal - $0.budget_gb) } ?? 8 }

    // ---- settings ----
    // Settings shows what in the scheduler's config file is the user's to decide; what sets a model or a tool up
    // (commands, paths, ports) is left to the file. While a value is edited it is text, kept under its place in
    // the file; Save turns what changed back into values and sends only that.
    enum Kind { case number, whole, text, flag }
    static let sep = "\u{1}"

    func key(_ path: [String]) -> String { path.joined(separator: Self.sep) }
    func field(_ path: [String]) -> Binding<String> {
        let k = key(path)
        return Binding(get: { self.form[k] ?? "" }, set: { self.form[k] = $0 })
    }
    func flag(_ path: [String]) -> Binding<Bool> {
        let k = key(path)
        return Binding(get: { self.form[k] == "true" }, set: { self.form[k] = $0 ? "true" : "false" })
    }
    var reserve: Double {
        get { number(form[key(["system_reserve_gb"])] ?? "") ?? savedReserve }
        set { form[key(["system_reserve_gb"])] = plain(newValue) }
    }
    var dirty: Bool { form != original }

    private func add(_ path: [String], _ kind: Kind, in root: [String: Any], absent: String = "") {
        var v: Any? = root
        for p in path { v = (v as? [String: Any])?[p] }
        let text: String
        switch (kind, v) {
        case (_, nil): text = absent
        case (.flag, let b as Bool): text = b ? "true" : "false"
        case (_, let s as String): text = s
        case (_, let n as NSNumber): text = plain(n.doubleValue)
        default: text = ""
        }
        kinds[key(path)] = kind
        form[key(path)] = text
    }

    /// Read the config file from the scheduler and fill the form.
    func loadConfig() async {
        guard isAdmin, let req = request("/scheduler/config"), let (data, _) = try? await URLSession.shared.data(for: req),
              let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let cfg = j["config"] as? [String: Any] else { return }
        form = [:]; kinds = [:]
        add(["system_reserve_gb"], .number, in: cfg)
        add(["manage_gpu_limit"], .flag, in: cfg, absent: "true")
        add(["listen", "host"], .text, in: cfg, absent: "127.0.0.1")
        add(["listen", "port"], .whole, in: cfg)
        add(["public_url"], .text, in: cfg)
        add(["starvation_after_s"], .number, in: cfg)
        for n in ((cfg["resources"] as? [String: Any]) ?? [:]).keys {
            for f in ["memory_gb", "keep_alive_s", "idle_stop_s"] { add(["resources", n, f], .number, in: cfg) }
        }
        add(["media", "public_url"], .text, in: cfg)
        original = form
        if cfg["system_reserve_gb"] == nil { reserve = savedReserve }  // an older config: saving moves it to the reserve
    }

    /// Fill the Settings from what the scheduler runs with now.
    func beginSettings() {
        saveNote = ""; saveFailed = false
        minted = ""; tokenNote = ""
        Task { await loadConfig(); await loadTokens() }
    }

    /// Send what was changed to the scheduler, which writes it into config.json and applies it at once.
    func saveSettings() async {
        var patch: [String: Any] = [:]
        func put(_ d: inout [String: Any], _ path: ArraySlice<String>, _ v: Any) {
            guard let first = path.first else { return }
            if path.count == 1 { d[first] = v; return }
            var inner = d[first] as? [String: Any] ?? [:]
            put(&inner, path.dropFirst(), v)
            d[first] = inner
        }
        for (k, text) in form where text != original[k] {
            let path = k.components(separatedBy: Self.sep), t = text.trimmingCharacters(in: .whitespacesAndNewlines)
            var value: Any = NSNull()  // an emptied field takes the setting out of the file, back to its default
            switch kinds[k] ?? .text {
            case .number, .whole:
                if !t.isEmpty {
                    guard let v = number(t), v >= 0 else {
                        saveNote = "\(path.dropFirst().joined(separator: " ")): \(t) is not a number."; saveFailed = true; return
                    }
                    value = v == v.rounded() ? Int(v) as Any : v as Any
                }
            case .flag: value = t == "true"
            case .text: if !t.isEmpty { value = t }
            }
            put(&patch, path[...], value)
        }
        guard !patch.isEmpty, let req = request("/scheduler/config", body: ["patch": patch]) else { return }
        saving = true
        defer { saving = false }
        do {
            let (data, resp) = try await URLSession.shared.data(for: req)
            let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if (resp as? HTTPURLResponse)?.statusCode == 200 {
                await refresh()
                await loadConfig()
                let changes = (j?["changes"] as? [String]) ?? []
                saveNote = changes.isEmpty ? "Saved." : "Saved: " + changes.joined(separator: "; ") + "."
                saveFailed = false
            } else {
                saveNote = ((j?["error"] as? [String: Any])?["message"] as? String) ?? "The scheduler did not accept the settings."
                saveFailed = true
            }
        } catch {
            saveNote = "Could not reach the scheduler."; saveFailed = true
        }
    }

    /// True when the scheduler wants to set the GPU memory limit but macOS has not been allowed to let it yet.
    var gpuNeedsPermission: Bool { status?.gpu_limit?.error?.contains("install-gpu-limit.sh") ?? false }

    /// The installer that comes with the scheduler (it says where), when the scheduler runs on this Mac.
    var gpuInstaller: String? {
        guard isLocal, let p = status?.gpu_limit?.installer, FileManager.default.fileExists(atPath: p) else { return nil }
        return p
    }

    /// Ask for the administrator password once (the normal macOS dialog) and install the helper that lets the
    /// scheduler set the GPU memory limit from then on.
    func allowGpuLimit() async {
        guard let script = gpuInstaller else { return }
        allowing = true; allowNote = ""
        let quoted = "'" + script.replacingOccurrences(of: "'", with: "'\\''") + "' '" + NSUserName() + "'"
        let source = "do shell script \"/bin/zsh " + quoted.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
            + "\" with prompt \"Pinecone wants to let the scheduler set the GPU memory limit.\" with administrator privileges"
        let failed: String? = await Task.detached {
            let p = Process(), err = Pipe()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
            p.arguments = ["-e", source]
            p.standardOutput = FileHandle.nullDevice
            p.standardError = err
            do { try p.run() } catch { return error.localizedDescription }
            p.waitUntilExit()
            if p.terminationStatus == 0 { return nil }
            let text = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            return text.contains("-128") ? "" : text.trimmingCharacters(in: .whitespacesAndNewlines)
        }.value
        allowing = false
        if let failed { allowNote = failed.isEmpty ? "" : "That did not work: \(failed)" }
        else { allowNote = "Allowed. The scheduler sets the limit within half a minute." }
        await refresh()
    }

    // ---- access tokens ----
    func loadTokens() async {
        guard isAdmin, let req = request("/scheduler/tokens"), let (data, _) = try? await URLSession.shared.data(for: req),
              let l = try? JSONDecoder().decode(TokenList.self, from: data) else { return }
        tokens = l.tokens
    }

    func mintToken() async {
        let name = tokenName.trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty, let req = request("/scheduler/mint", body: ["name": name]) else { return }
        tokenNote = ""
        guard let (data, _) = try? await URLSession.shared.data(for: req),
              let j = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let tok = j["token"] as? String else {
            tokenNote = "The scheduler did not make a token."; return
        }
        minted = tok; mintedName = name; tokenName = ""; copied = false
        await loadTokens()
    }

    func revokeToken(_ id: String) async {
        guard let req = request("/scheduler/revoke", body: ["id": id]) else { return }
        _ = try? await URLSession.shared.data(for: req)
        await loadTokens()
    }

    func copyMinted() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(minted, forType: .string)
        copied = true
    }

    func unload(_ name: String) async {
        guard let req = request("/scheduler/unload", body: ["resource": name]) else { return }
        _ = try? await URLSession.shared.data(for: req)
        await refresh()
    }

    /// Start or stop the launchd services (see serviceLabels).
    func setServices(running: Bool) async {
        serviceBusy = true
        let uid = getuid()
        let labels = self.labels
        await Task.detached {
            for label in labels {
                let plist = NSHomeDirectory() + "/Library/LaunchAgents/\(label).plist"
                let args = running ? ["bootstrap", "gui/\(uid)", plist] : ["bootout", "gui/\(uid)/\(label)"]
                let p = Process()
                p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
                p.arguments = args
                p.standardOutput = FileHandle.nullDevice
                p.standardError = FileHandle.nullDevice
                try? p.run()
                p.waitUntilExit()
            }
        }.value
        try? await Task.sleep(nanoseconds: 1_500_000_000)
        await refresh()
        serviceBusy = false
    }
}
