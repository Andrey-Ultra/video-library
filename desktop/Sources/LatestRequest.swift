import Foundation

/// Only the latest asynchronous read may update its owner's state.
struct LatestRequest {
  private var revision: UInt = 0
  mutating func begin() -> UInt {
    revision &+= 1
    return revision
  }
  mutating func invalidate() { _ = begin() }
  func accepts(_ request: UInt) -> Bool { request == revision }
}
