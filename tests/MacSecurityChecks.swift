// swiftc macos_app/Sources/{MacStorage,MacNetwork}.swift tests/MacSecurityChecks.swift -o /tmp/check-easycolab-mac
import Foundation

@main
struct MacSecurityChecks {
    static func main() throws {
        let official = URL(string: "https://huggingface.co/api/models")!
        let mirror = URL(string: "https://hf-mirror.com/api/models")!
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let original = HFCatalogRequests.request(official, token: "test-token", timeout: 1)
        precondition(HFCatalogRequests.hosts(token: "test-token") == ["https://huggingface.co"])
        precondition(HFCatalogRequests.hosts(token: "").contains("https://hf-mirror.com"))
        precondition(original.value(forHTTPHeaderField: "Authorization") == "Bearer test-token")
        precondition(HFCatalogRequests.request(mirror, token: "test-token", timeout: 1)
            .value(forHTTPHeaderField: "Authorization") == nil)
        let task = session.dataTask(with: original)
        let response = HTTPURLResponse(url: official, statusCode: 302, httpVersion: nil, headerFields: nil)!
        var proposedRedirect = original
        proposedRedirect.url = mirror
        HFCatalogRequests().urlSession(session, task: task, willPerformHTTPRedirection: response,
                                      newRequest: proposedRedirect) { redirect in
            precondition(redirect == nil)
        }
        proposedRedirect.setValue("test-token", forHTTPHeaderField: "X-HuggingFace-Token")
        SameOriginAdminRedirect().urlSession(session, task: task, willPerformHTTPRedirection: response,
                                            newRequest: proposedRedirect) { precondition($0 == nil) }

        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try fm.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: directory) }
        let input = directory.appendingPathComponent("source")
        let replacement = Data("new media".utf8)
        try replacement.write(to: input)
        let expected = directory.appendingPathComponent("图像.png")
        try Data("old media".utf8).write(to: expected)
        let saved = try LocalMediaDownload.save(input, filename: "图像.png", in: directory)
        let savedData = try Data(contentsOf: saved)
        precondition(saved == expected && savedData == replacement)
        for name in ["", "nested/media.png", ".hidden.png", "document.txt", "bad\nname.mp4"] {
            do {
                _ = try LocalMediaDownload.save(input, filename: name, in: directory)
                preconditionFailure("Expected invalid filename to be rejected")
            } catch let error as CocoaError { precondition(error.code == .fileWriteInvalidFileName) }
        }
        let directoryTarget = directory.appendingPathComponent("existing.mp4")
        try fm.createDirectory(at: directoryTarget, withIntermediateDirectories: false)
        do {
            _ = try LocalMediaDownload.save(input, filename: "existing.mp4", in: directory)
            preconditionFailure("Expected an existing directory to be preserved")
        } catch is POSIXError {}
        var isDirectory: ObjCBool = false
        precondition(fm.fileExists(atPath: directoryTarget.path, isDirectory: &isDirectory) && isDirectory.boolValue)
        print("macOS credential routing and media storage checks passed")
    }
}
