import AppKit
import CoreGraphics
import CoreVideo
import Foundation
import QuartzCore

// A display-cadence canary. It measures this helper's callback arrival gaps;
// it does not claim to measure another application's renderer directly.
final class DisplayGapWindow: @unchecked Sendable {
    private let lock = NSLock()
    private var lastArrival: CFTimeInterval?
    private var samples: [(arrival: CFTimeInterval, gapMS: Double)] = []

    func record(arrival now: CFTimeInterval) {
        lock.lock()
        defer { lock.unlock() }

        if let previous = lastArrival {
            let gap = now - previous
            if gap > 0, gap < 5.0 {
                samples.append((now, gap * 1_000.0))
            } else {
                // Ignore sleep, display reconfiguration, and debugger gaps.
                samples.removeAll(keepingCapacity: true)
            }
        }
        lastArrival = now
        samples.removeAll { now - $0.arrival > 5.0 }
    }

    func p95MS(now: CFTimeInterval) -> Double? {
        lock.lock()
        defer { lock.unlock() }

        guard let lastArrival, now - lastArrival < 1.0, samples.count >= 5 else {
            return nil
        }
        let values = samples.map(\.gapMS).sorted()
        let index = max(0, Int(ceil(Double(values.count) * 0.95)) - 1)
        return values[index]
    }

    func maximumRecentGapMS(now: CFTimeInterval) -> Double? {
        lock.lock()
        defer { lock.unlock() }

        return samples
            .filter { now - $0.arrival <= 2.0 }
            .map(\.gapMS)
            .max()
    }

    func callbackAgeMS(now: CFTimeInterval) -> Double? {
        lock.lock()
        defer { lock.unlock() }

        guard let lastArrival else { return nil }
        return max(0.0, (now - lastArrival) * 1_000.0)
    }

    func reset() {
        lock.lock()
        defer { lock.unlock() }
        lastArrival = nil
        samples.removeAll(keepingCapacity: true)
    }
}

private func thermalStateName(_ state: ProcessInfo.ThermalState) -> String {
    switch state {
    case .nominal: return "nominal"
    case .fair: return "fair"
    case .serious: return "serious"
    case .critical: return "critical"
    @unknown default: return "unknown"
    }
}

private func legacyDisplayLinkCallback(
    _ displayLink: CVDisplayLink,
    _ inNow: UnsafePointer<CVTimeStamp>,
    _ inOutputTime: UnsafePointer<CVTimeStamp>,
    _ flagsIn: CVOptionFlags,
    _ flagsOut: UnsafeMutablePointer<CVOptionFlags>,
    _ userInfo: UnsafeMutableRawPointer?
) -> CVReturn {
    guard let userInfo else { return kCVReturnInvalidArgument }
    let gaps = Unmanaged<DisplayGapWindow>.fromOpaque(userInfo).takeUnretainedValue()
    gaps.record(arrival: CACurrentMediaTime())
    return kCVReturnSuccess
}

final class GuardianProbe: NSObject {
    private let gaps = DisplayGapWindow()
    private var retainedModernDisplayLink: AnyObject?
    private var legacyDisplayLink: CVDisplayLink?

    @objc func resetCadence() {
        gaps.reset()
    }

    private func stopDisplayLink() {
        if #available(macOS 14.0, *),
           let link = retainedModernDisplayLink as? CADisplayLink {
            link.invalidate()
        }
        retainedModernDisplayLink = nil
        if let link = legacyDisplayLink {
            CVDisplayLinkStop(link)
            legacyDisplayLink = nil
        }
    }

    @objc func rebuildDisplayLink() {
        stopDisplayLink()
        gaps.reset()
        startDisplayLink()
    }

    func startDisplayLink() {
        if #available(macOS 14.0, *),
           let screen = NSScreen.main ?? NSScreen.screens.first {
            let link = screen.displayLink(
                target: self,
                selector: #selector(modernDisplayLinkTick(_:))
            )
            link.add(to: .main, forMode: .common)
            retainedModernDisplayLink = link
            return
        }

        var link: CVDisplayLink?
        guard CVDisplayLinkCreateWithActiveCGDisplays(&link) == kCVReturnSuccess,
              let link else { return }
        let context = Unmanaged.passUnretained(gaps).toOpaque()
        guard CVDisplayLinkSetOutputCallback(
            link,
            legacyDisplayLinkCallback,
            context
        ) == kCVReturnSuccess,
        CVDisplayLinkStart(link) == kCVReturnSuccess else { return }
        legacyDisplayLink = link
    }

    @available(macOS 14.0, *)
    @objc private func modernDisplayLinkTick(_ link: CADisplayLink) {
        gaps.record(arrival: CACurrentMediaTime())
    }

    func emitNDJSON() {
        // kCGAnyInputEventType is the C macro ((CGEventType)(~0)); C macros do
        // not import into Swift, so use its exact UInt32 raw value.
        let anyInputEvent = CGEventType(rawValue: UInt32.max)!
        let inputIdle = CGEventSource.secondsSinceLastEventType(
            .combinedSessionState,
            eventType: anyInputEvent
        )

        let screen = NSScreen.main ?? NSScreen.screens.first
        // These are both awake-time seconds since boot, not wall-clock time.
        let frameAgeMS = screen.map {
            max(
                0.0,
                (ProcessInfo.processInfo.systemUptime
                    - $0.lastDisplayUpdateTimestamp) * 1_000.0
            )
        }
        let maximumRefreshIntervalMS = screen.map {
            $0.maximumRefreshInterval * 1_000.0
        }
        let displayNow = CACurrentMediaTime()
        let displayP95MS = gaps.p95MS(now: displayNow)
        let displayMaximumGapMS = gaps.maximumRecentGapMS(now: displayNow)
        let displayCallbackAgeMS = gaps.callbackAgeMS(now: displayNow)
        let frontmostBundleID = NSWorkspace.shared.frontmostApplication?.bundleIdentifier
        let processInfo = ProcessInfo.processInfo
        let lowPowerModeEnabled: Any
        if #available(macOS 12.0, *) {
            lowPowerModeEnabled = processInfo.isLowPowerModeEnabled
        } else {
            lowPowerModeEnabled = NSNull()
        }

        let object: [String: Any] = [
            "input_idle_seconds": inputIdle,
            "frame_age_ms": frameAgeMS ?? NSNull(),
            "maximum_refresh_interval_ms": maximumRefreshIntervalMS ?? NSNull(),
            "display_link_p95_ms": displayP95MS ?? NSNull(),
            "display_link_max_gap_ms": displayMaximumGapMS ?? NSNull(),
            "display_link_callback_age_ms": displayCallbackAgeMS ?? NSNull(),
            "frontmost_bundle_id": frontmostBundleID ?? NSNull(),
            "thermal_state": thermalStateName(processInfo.thermalState),
            "low_power_mode_enabled": lowPowerModeEnabled,
            "sample_uptime": processInfo.systemUptime,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let newline = "\n".data(using: .utf8) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(newline)
    }
}

// Connect to WindowServer without activating, creating a window, showing a
// Dock icon, or requesting Accessibility/Screen Recording permission.
let application = NSApplication.shared
_ = application.setActivationPolicy(.prohibited)

let probe = GuardianProbe()
probe.startDisplayLink()
probe.emitNDJSON()

// Sleep/wake and display reconfiguration gaps are not foreground jank. Clear
// the short cadence window so the first post-wake sample cannot trigger Pause.
NSWorkspace.shared.notificationCenter.addObserver(
    probe,
    selector: #selector(GuardianProbe.resetCadence),
    name: NSWorkspace.didWakeNotification,
    object: nil
)
NotificationCenter.default.addObserver(
    probe,
    selector: #selector(GuardianProbe.rebuildDisplayLink),
    name: NSApplication.didChangeScreenParametersNotification,
    object: nil
)

Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
    probe.emitNDJSON()
}
RunLoop.main.run()
