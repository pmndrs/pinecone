// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Pinecone",
    platforms: [.macOS(.v14)],
    targets: [.executableTarget(name: "Pinecone", path: "Sources/Pinecone")]
)
