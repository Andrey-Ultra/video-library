import Foundation

actor ResponseGate {
  private var pending: [String: CheckedContinuation<Void, Never>] = [:]
  func wait(_ name: String) async {
    await withCheckedContinuation { pending[name] = $0 }
  }
  func isWaiting(_ name: String) -> Bool { pending[name] != nil }
  func release(_ name: String) { pending.removeValue(forKey: name)?.resume() }
}

@MainActor final class InventoryProbe {
  var current = "initial"
  var requests = LatestRequest()
  func refresh(_ response: String, gate: ResponseGate) async {
    let request = requests.begin()
    await gate.wait(response)
    guard requests.accepts(request), !Task.isCancelled else { return }
    current = response
  }
}

@main struct RequestChecks {
  @MainActor static func main() async {
    let probe = InventoryProbe()
    let gate = ResponseGate()
    let old = Task { await probe.refresh("old-ready", gate: gate) }
    while !(await gate.isWaiting("old-ready")) { await Task.yield() }
    let new = Task { await probe.refresh("new-missing", gate: gate) }
    while !(await gate.isWaiting("new-missing")) { await Task.yield() }
    await gate.release("new-missing")
    await new.value
    await gate.release("old-ready")
    await old.value
    precondition(probe.current == "new-missing", "An old response overwrote newer model status")

    let closing = Task { await probe.refresh("after-close", gate: gate) }
    while !(await gate.isWaiting("after-close")) { await Task.yield() }
    probe.requests.invalidate()
    await gate.release("after-close")
    await closing.value
    precondition(probe.current == "new-missing", "Closing failed to invalidate the in-flight response")

    let cancelled = Task { await probe.refresh("cancelled", gate: gate) }
    while !(await gate.isWaiting("cancelled")) { await Task.yield() }
    cancelled.cancel()
    await gate.release("cancelled")
    await cancelled.value
    precondition(probe.current == "new-missing", "A cancelled read updated inventory")
    print("PASS: out-of-order responses, invalidation and cancelled reads")
  }
}
