import Darwin
import Foundation
import Security

enum MacCredentials {
    private static let service = "app.easycolab.studio.macos.credentials"

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    static func read(_ account: String) throws -> String? {
        var attributes = query(account)
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(attributes as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
        guard let data = result as? Data, let value = String(data: data, encoding: .utf8) else {
            throw CocoaError(.fileReadInapplicableStringEncoding)
        }
        return value
    }

    static func save(_ value: String, for account: String) throws {
        let attributes = query(account)
        var status: OSStatus
        if value.isEmpty {
            status = SecItemDelete(attributes as CFDictionary)
            if status == errSecItemNotFound { return }
        } else {
            let data = [kSecValueData as String: Data(value.utf8)]
            status = SecItemUpdate(attributes as CFDictionary, data as CFDictionary)
            if status == errSecItemNotFound {
                status = SecItemAdd(attributes.merging(data) { _, new in new } as CFDictionary, nil)
                if status == errSecDuplicateItem {
                    status = SecItemUpdate(attributes as CFDictionary, data as CFDictionary)
                }
            }
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }

    // Preserve the first (most recent) credential and clear each plaintext copy only
    // after Keychain has accepted it. A locked Keychain is retried on the next launch.
    static func migratePreferences(in domain: String, defaults: UserDefaults) {
        guard let values = defaults.persistentDomain(forName: domain) else { return }
        func migrate(_ value: String, to account: String) -> Bool {
            do {
                if !value.isEmpty, try read(account) == nil { try save(value, for: account) }
                return true
            } catch { return false }
        }
        for key in ["apiAccessKey", "hfToken"] {
            if let value = values[key] as? String, migrate(value, to: key) {
                defaults.removeObject(forKey: key)
            }
        }
        if let passwords = values["adminPasswordsByURL"] as? [String: String] {
            var remaining = passwords
            for (address, password) in passwords {
                let account = adminAccount(address)
                if migrate(password, to: account) { remaining.removeValue(forKey: address) }
            }
            if remaining.isEmpty {
                defaults.removeObject(forKey: "adminPasswordsByURL")
            } else if remaining.count != passwords.count {
                defaults.set(remaining, forKey: "adminPasswordsByURL")
            }
        }
    }

    static func adminAccount(_ address: String) -> String {
        "admin:" + address.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(
            of: "/+$", with: "", options: .regularExpression
        )
    }
}

enum LocalMediaDownload {
    private static let extensions: Set<String> = ["mp4", "webm", "mov", "webp", "gif", "png", "jpg", "jpeg"]

    static func save(_ source: URL, filename: String, in directory: URL, prefix: String = "") throws -> URL {
        guard !filename.isEmpty, !filename.hasPrefix("."), !filename.contains("/"),
              !filename.contains("\\"), !filename.contains(":"),
              filename.rangeOfCharacter(from: .controlCharacters) == nil,
              extensions.contains((filename as NSString).pathExtension.lowercased()) else {
            throw CocoaError(.fileWriteInvalidFileName)
        }
        let name = prefix + filename
        let root = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard root >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(root) }

        // Stage in the destination directory so replacement is atomic across volumes.
        // Relative descriptor operations never follow a replacement of the directory.
        let staging = ".easycolab-" + UUID().uuidString
        let output = openat(root, staging, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0o600)
        guard output >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(output); unlinkat(root, staging, 0) }
        let input = try FileHandle(forReadingFrom: source)
        defer { try? input.close() }
        guard fcopyfile(input.fileDescriptor, output, nil, copyfile_flags_t(COPYFILE_DATA)) == 0,
              renameat(root, staging, root, name) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        return directory.appendingPathComponent(name, isDirectory: false)
    }
}
