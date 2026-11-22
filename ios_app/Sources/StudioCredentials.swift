import Foundation
import Security

enum StudioCredentials {
    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "app.easycolab.studio.ios.credentials",
         kSecAttrAccount as String: account]
    }

    static func read(_ account: String) -> String {
        var lookup = query(account)
        lookup[kSecReturnData as String] = true
        var item: CFTypeRef?
        guard SecItemCopyMatching(lookup as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(decoding: data, as: UTF8.self)
    }

    @discardableResult
    static func write(_ value: String, for account: String) -> Bool {
        let lookup = query(account)
        if value.isEmpty {
            let status = SecItemDelete(lookup as CFDictionary)
            return status == errSecSuccess || status == errSecItemNotFound
        }
        let change = [kSecValueData as String: Data(value.utf8)]
        let status = SecItemUpdate(lookup as CFDictionary, change as CFDictionary)
        if status != errSecItemNotFound { return status == errSecSuccess }
        var item = lookup.merging(change) { _, new in new }
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(item as CFDictionary, nil) == errSecSuccess
    }

    static func migrate(_ key: String) -> String {
        let saved = read(key)
        guard let old = UserDefaults.standard.string(forKey: key) else { return saved }
        let value = saved.isEmpty ? old : saved
        if write(value, for: key) { UserDefaults.standard.removeObject(forKey: key) }
        return value
    }

    static func origin(_ url: URL?) -> String? {
        guard let url, url.scheme?.lowercased() == "https", let host = url.host?.lowercased(),
              url.user == nil, url.password == nil else { return nil }
        return "https://\(host):\(url.port ?? 443)"
    }
}

// Do not replay credentials or request bodies to redirected origins.
final class StudioRequestRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        guard let origin = StudioCredentials.origin(task.originalRequest?.url),
              origin == StudioCredentials.origin(request.url) else { completionHandler(nil); return }
        completionHandler(request)
    }
}
