import Foundation

final class SameOriginAdminRedirect: NSObject, URLSessionTaskDelegate {
    static func permits(_ target: URL?, from origin: URL?) -> Bool {
        guard let target, let origin, let host = origin.host, !host.isEmpty,
              target.scheme?.lowercased() == "https", origin.scheme?.lowercased() == "https",
              target.user == nil, target.password == nil, origin.user == nil, origin.password == nil else { return false }
        return target.host?.lowercased() == host.lowercased() && (target.port ?? 443) == (origin.port ?? 443)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let original = task.originalRequest, Self.permits(request.url, from: original.url) else {
            completionHandler(nil)
            return
        }
        var redirected = request
        redirected.setValue(original.value(forHTTPHeaderField: "Authorization"), forHTTPHeaderField: "Authorization")
        redirected.setValue(original.value(forHTTPHeaderField: "X-HuggingFace-Token"), forHTTPHeaderField: "X-HuggingFace-Token")
        completionHandler(redirected)
    }
}

final class HFCatalogRequests: NSObject, URLSessionTaskDelegate {
    private static let official = URL(string: "https://huggingface.co")!
    static func hosts(token: String) -> [String] {
        token.isEmpty ? [official.absoluteString, "https://hf-mirror.com"] : [official.absoluteString]
    }
    static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.httpShouldSetCookies = false
        config.urlCredentialStorage = nil
        return URLSession(configuration: config, delegate: HFCatalogRequests(), delegateQueue: nil)
    }()

    static func request(_ url: URL, token: String, timeout: TimeInterval) -> URLRequest {
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        request.setValue("EasyColab-macOS/2.0", forHTTPHeaderField: "User-Agent")
        if !token.isEmpty, SameOriginAdminRedirect.permits(url, from: official) {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        guard let url = request.url, url.scheme?.lowercased() == "https", url.user == nil, url.password == nil else {
            completionHandler(nil)
            return
        }
        let authorization = task.originalRequest?.value(forHTTPHeaderField: "Authorization")
        if authorization != nil, !SameOriginAdminRedirect.permits(url, from: Self.official) {
            completionHandler(nil)
            return
        }
        var redirected = request
        redirected.setValue(authorization, forHTTPHeaderField: "Authorization")
        completionHandler(redirected)
    }
}
