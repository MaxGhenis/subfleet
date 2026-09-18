// Subfleet — menu bar app over the subfleet monitor state.
//
// Reads ~/chief-of-staff/state/subfleet/{snapshot.json, claude-statusline.json,
// alerts.json} (written by the launchd watchdog every 30 min + statusline tap);
// never probes the network itself. "Refresh" kickstarts the watchdog so live
// probing, alerting, and this app all share one pipeline. Honest by design:
// anything unprobeable renders as "unknown"/"not enrolled", never a number.
//
// Build: ./build.sh   (swiftc single file -> ~/Applications/Subfleet.app)

import AppKit
import ServiceManagement
import SwiftUI

let stateDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("chief-of-staff/state/subfleet")

// MARK: - Model (tolerant JSON: schema evolves, so everything is optional)

struct Window_: Decodable {
    var used_percent: Double?
    var reset_at: String?
}

struct Windows: Decodable {
    var primary: Window_?
    var secondary: Window_?
    var source: String?
    var as_of: String?
}

struct CodexHome: Decodable {
    var home: String
    var email: String?
    var verdict: String
    var windows: Windows?
    var duplicate_of: String?
}

struct Fleet: Decodable {
    var total_homes: Int
    var dispatchable_now: Int
    var best_home: String?
    var earliest_reset: String?
}

struct CodexSection: Decodable {
    var homes: [CodexHome]
    var fleet: Fleet
}

struct OAuthWindow: Decodable {
    var used_percent: Double?
    var reset_at: Double?
}

struct AccountProbe: Decodable {
    var status: String?
    var five_hour: OAuthWindow?
    var seven_day: OAuthWindow?
}

struct StatuslineData: Decodable {
    var five_hour_pct: Double?
    var seven_day_pct: Double?
    var fresh: Bool?
    var updated_at: String?
}

struct LiveUsage: Decodable {
    var five_hour_pct: Double?
    var seven_day_pct: Double?
    var model_weeks: [String: Double]?
    var source: String?
}

struct ClaudeAccount: Decodable {
    var email: String
    var active: Bool
    var enrolled: Bool
    var probe: AccountProbe?
    var statusline: StatuslineData?
    var live: LiveUsage?
    var oauth_status: String?
}

struct ActiveLimit: Decodable {
    var kind: String?
    var reset_at: String?
}

struct ClaudeSection: Decodable {
    var accounts: [ClaudeAccount]?
    var statusline: StatuslineData?
    var active_limit: ActiveLimit?
    var tier: String?
    var account: ClaudeIdent?
}

struct ClaudeIdent: Decodable { var email: String? }

struct Snapshot: Decodable {
    var generated_at: String
    var codex: CodexSection
    var claude: ClaudeSection
}

// MARK: - Store

final class QuotaStore: ObservableObject {
    @Published var snap: Snapshot?
    @Published var loadedAt = Date()
    @Published var refreshing = false
    private var timer: Timer?

    init() {
        load()
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            self?.load()
        }
    }

    func load() {
        let url = stateDir.appendingPathComponent("snapshot.json")
        guard let data = try? Data(contentsOf: url),
              var snap = try? JSONDecoder().decode(Snapshot.self, from: data)
        else {
            DispatchQueue.main.async { self.snap = nil }
            return
        }
        // Fresher statusline beats the snapshot's embedded copy for the active account.
        let slURL = stateDir.appendingPathComponent("claude-statusline.json")
        if let slData = try? Data(contentsOf: slURL),
           let obj = try? JSONSerialization.jsonObject(with: slData) as? [String: Any],
           let rl = obj["rate_limits"] as? [String: Any] {
            func pct(_ key: String) -> Double? {
                ((rl[key] as? [String: Any])?["used_percentage"] as? NSNumber)?.doubleValue
            }
            var sl = StatuslineData()
            sl.five_hour_pct = pct("five_hour")
            sl.seven_day_pct = pct("seven_day")
            sl.updated_at = obj["updated_at"] as? String
            if sl.five_hour_pct != nil { snap.claude.statusline = sl }
        }
        DispatchQueue.main.async {
            self.snap = snap
            self.loadedAt = Date()
        }
    }

    /// Kickstart the launchd watchdog (live probe + alerts + fresh snapshot).
    func refresh() {
        refreshing = true
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = ["kickstart", "gui/\(getuid())/com.maxghenis.cos.subfleet"]
            try? p.run()
            p.waitUntilExit()
            // Snapshot lands when probes finish; poll briefly.
            for _ in 0..<20 {
                Thread.sleep(forTimeInterval: 1.5)
                self.load()
            }
            DispatchQueue.main.async { self.refreshing = false }
        }
    }

    // Menu bar label: "4/4" lanes, plus a warning glyph on any problem.
    var barLabel: String {
        guard let s = snap else { return "–/–" }
        return "\(s.codex.fleet.dispatchable_now)/\(s.codex.fleet.total_homes)"
    }

    var hasProblem: Bool {
        guard let s = snap else { return true }
        let badHome = s.codex.homes.contains {
            ["auth-revoked", "no-auth", "auth-suspect"].contains($0.verdict)
        }
        return badHome || s.claude.active_limit != nil || s.codex.fleet.dispatchable_now == 0
    }
}

// MARK: - Formatting helpers

func clock(_ iso: String?) -> String {
    guard let iso, let date = ISO8601DateFormatter().date(from: iso) else { return "?" }
    let f = DateFormatter()
    let near = Calendar.current.isDateInToday(date) || Calendar.current.isDateInTomorrow(date)
    f.dateFormat = near ? "h:mma" : "h:mma EEE"
    return f.string(from: date).lowercased()
}

func shortHome(_ home: String) -> String {
    home.replacingOccurrences(of: NSHomeDirectory(), with: "~")
}

func verdictColor(_ v: String) -> Color {
    switch v {
    case "ok": return .green
    case "limited": return .orange
    default: return .red
    }
}

// MARK: - Views

struct UsageBar: View {
    let pct: Double
    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(.quaternary)
                Capsule()
                    .fill(pct >= 95 ? Color.red : pct >= 75 ? .orange : .green)
                    .frame(width: max(3, geo.size.width * min(pct, 100) / 100))
            }
        }
        .frame(width: 56, height: 5)
    }
}

struct LaneRow: View {
    let name: String
    let detail: String
    let pct: Double?
    let trailing: String
    let color: Color

    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(color).frame(width: 7, height: 7)
            VStack(alignment: .leading, spacing: 1) {
                Text(name).font(.system(.body, design: .rounded).weight(.medium))
                if !detail.isEmpty {
                    Text(detail).font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            if let pct {
                Text("\(Int(pct.rounded()))%")
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                UsageBar(pct: pct)
            }
            Text(trailing)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 76, alignment: .trailing)
        }
    }
}

struct ContentView: View {
    @ObservedObject var store: QuotaStore
    @State private var loginItem = SMAppService.mainApp.status == .enabled

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let snap = store.snap {
                header(snap)
                Divider()
                codexSection(snap)
                Divider()
                claudeSection(snap)
            } else {
                Text("No snapshot yet — is the subfleet watchdog loaded?")
                    .font(.callout).foregroundStyle(.secondary)
                Text("launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.maxghenis.cos.subfleet.plist")
                    .font(.system(.caption2, design: .monospaced)).textSelection(.enabled)
            }
            Divider()
            footer
        }
        .padding(12)
        .frame(width: 340)
    }

    @ViewBuilder
    func header(_ snap: Snapshot) -> some View {
        HStack {
            Text("AI quota").font(.headline)
            Spacer()
            Text("as of \(clock(snap.generated_at))")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    func codexSection(_ snap: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("CODEX").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                if let best = snap.codex.fleet.best_home {
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("CODEX_HOME=\(best) ", forType: .string)
                    } label: {
                        Label("copy best: \(shortHome(best))", systemImage: "doc.on.doc")
                            .font(.caption2)
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(.blue)
                    .help("Copy CODEX_HOME=… for dispatch")
                }
            }
            ForEach(snap.codex.homes, id: \.home) { h in
                let pct = h.windows?.primary?.used_percent
                let stale = h.windows?.source == "observed"
                LaneRow(
                    name: shortHome(h.home),
                    detail: (h.email ?? "?") + (h.duplicate_of != nil ? "  ⚠ duplicate" : ""),
                    pct: pct,
                    trailing: h.verdict == "ok" || h.verdict == "limited"
                        ? "→ \(clock(h.windows?.primary?.reset_at))" + (stale ? " *" : "")
                        : h.verdict.uppercased(),
                    color: verdictColor(h.verdict)
                )
            }
        }
    }

    @ViewBuilder
    func claudeSection(_ snap: Snapshot) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("CLAUDE").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
            let accounts = snap.claude.accounts ?? []
            ForEach(accounts, id: \.email) { a in
                accountRow(a, snap: snap)
            }
            if accounts.isEmpty {
                Text("no account data in snapshot").font(.caption).foregroundStyle(.secondary)
            }
            if let limit = snap.claude.active_limit {
                Label(
                    "\(limit.kind ?? "limit") active — resets \(clock(limit.reset_at))",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption).foregroundStyle(.orange)
            }
            let unenrolled = accounts.filter { !$0.active && !$0.enrolled }.count
            if unenrolled > 0 {
                Text("\(unenrolled) accounts not enrolled — claude setup-token → subfleet enroll <email>")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
    }

    @ViewBuilder
    func accountRow(_ a: ClaudeAccount, snap: Snapshot) -> some View {
        // Show a row when we have real data for it (active w/ statusline, or enrolled probe).
        if a.active {
            let sl = a.statusline ?? snap.claude.statusline
            let fiveHour = a.live?.five_hour_pct ?? sl?.five_hour_pct
            let sevenDay = a.live?.seven_day_pct ?? sl?.seven_day_pct
            let noDataHint = a.oauth_status == "rate-limited" ? "429/limited" : "5h unknown"
            let modelWeeks = (a.live?.model_weeks ?? [:])
                .sorted { $0.value > $1.value }
                .map { "\($0.key) wk \(Int($0.value.rounded()))%" }
                .joined(separator: " · ")
            LaneRow(
                name: a.email,
                detail: "active login"
                    + (snap.claude.tier.map { " · \($0)" } ?? "")
                    + (modelWeeks.isEmpty ? "" : " · \(modelWeeks)"),
                pct: fiveHour,
                trailing: sevenDay.map { "wk \(Int($0.rounded()))%" } ?? noDataHint,
                color: snap.claude.active_limit != nil ? .orange
                    : fiveHour != nil ? .green : .gray
            )
        } else if a.enrolled {
            let ok = a.probe?.status == "ok"
            LaneRow(
                name: a.email,
                detail: "enrolled",
                pct: a.probe?.five_hour?.used_percent,
                trailing: ok
                    ? (a.probe?.seven_day?.used_percent).map { "wk \(Int($0.rounded()))%" } ?? "ok"
                    : (a.probe?.status ?? "?"),
                color: ok ? .green : .red
            )
        }
    }

    var footer: some View {
        HStack {
            Button {
                store.refresh()
            } label: {
                if store.refreshing {
                    Label("Probing…", systemImage: "arrow.triangle.2.circlepath")
                } else {
                    Label("Refresh (live probe)", systemImage: "arrow.clockwise")
                }
            }
            .disabled(store.refreshing)
            Spacer()
            Toggle("Start at login", isOn: $loginItem)
                .toggleStyle(.checkbox)
                .font(.caption)
                .onChange(of: loginItem) { _, on in
                    try? on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister()
                }
                .help("Launch at login")
            Button {
                NSApp.terminate(nil)
            } label: {
                Image(systemName: "power")
            }
            .buttonStyle(.plain)
            .help("Quit Subfleet")
        }
        .font(.callout)
    }
}

// MARK: - App

@main
struct SubfleetApp: App {
    @StateObject private var store = QuotaStore()

    var body: some Scene {
        MenuBarExtra {
            ContentView(store: store)
        } label: {
            HStack(spacing: 2) {
                Image(systemName: store.hasProblem ? "bolt.trianglebadge.exclamationmark" : "bolt.fill")
                Text(store.barLabel).font(.system(.body, design: .monospaced))
            }
        }
        .menuBarExtraStyle(.window)
    }
}
