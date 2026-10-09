// h3-translate: translate with the engine built into macOS (on device, no network). Used by plugins/macos_translate.py.
// Usage: h3-translate <source> <target>           reads a JSON array of strings on stdin, writes the translated array to stdout
//        h3-translate --status <source> <target>  only reports whether this language pair is installed
import Foundation
import Translation

@main
struct Main {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        let statusOnly = args.first == "--status"
        if statusOnly { args.removeFirst() }
        guard args.count == 2 else { FileHandle.standardError.write("usage: h3-translate [--status] <src> <dst>\n".data(using: .utf8)!); exit(2) }
        let src = Locale.Language(identifier: args[0]), dst = Locale.Language(identifier: args[1])
        let status = await LanguageAvailability().status(from: src, to: dst)
        if statusOnly {
            switch status { case .installed: print("installed"); case .supported: print("supported-not-installed"); default: print("unsupported") }
            return
        }
        guard status == .installed else {
            FileHandle.standardError.write("language pack not installed: \(args[0]) -> \(args[1])\n".data(using: .utf8)!); exit(3)
        }
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard let texts = try? JSONDecoder().decode([String].self, from: input) else {
            FileHandle.standardError.write("stdin must be a JSON array of strings\n".data(using: .utf8)!); exit(2)
        }
        do {
            let session = TranslationSession(installedSource: src, target: dst)
            var out: [String] = []
            for t in texts {
                if t.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { out.append(t); continue }
                out.append(try await session.translate(t).targetText)
            }
            FileHandle.standardOutput.write(try JSONEncoder().encode(out))
        } catch {
            FileHandle.standardError.write("translate failed: \(error)\n".data(using: .utf8)!); exit(1)
        }
    }
}
