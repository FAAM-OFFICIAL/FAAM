// Generates the FAAM app-icon PNGs (the gradient bar-chart mark on a dark,
// rounded square) into an .iconset folder. Usage: makeicon <iconset-dir>
import Cocoa
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

func drawIcon(size: CGFloat) -> CGImage {
    let cs = CGColorSpaceCreateDeviceRGB()
    let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8,
                        bytesPerRow: 0, space: cs,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    ctx.setShouldAntialias(true)
    ctx.interpolationQuality = .high

    // Dark rounded-square background (macOS-style), matching FAAM's theme.
    let radius = size * 0.205
    let bg = CGPath(roundedRect: CGRect(x: 0, y: 0, width: size, height: size),
                    cornerWidth: radius, cornerHeight: radius, transform: nil)
    ctx.addPath(bg)
    ctx.setFillColor(CGColor(red: 0.051, green: 0.063, blue: 0.094, alpha: 1)) // #0d1018
    ctx.fillPath()

    // FAAM mark — path "M3 21 V3 h2 v14 l5-7 4 5 7-10 v16 H3 z" in a 24x24 box.
    let pts: [(CGFloat, CGFloat)] = [
        (3,21),(3,3),(5,3),(5,17),(10,10),(14,15),(21,5),(21,21),(3,21)
    ]
    let inset = size * 0.12
    let box = size - inset * 2
    func map(_ p: (CGFloat, CGFloat)) -> CGPoint {
        CGPoint(x: p.0 / 24 * box + inset, y: size - (p.1 / 24 * box + inset)) // flip y for CG
    }
    let mark = CGMutablePath()
    mark.move(to: map(pts[0]))
    for p in pts.dropFirst() { mark.addLine(to: map(p)) }
    mark.closeSubpath()

    ctx.saveGState()
    ctx.addPath(mark)
    ctx.clip()
    let grad = CGGradient(colorsSpace: cs,
        colors: [CGColor(red: 0.31, green: 0.55, blue: 1.0, alpha: 1),    // #4f8cff
                 CGColor(red: 0.486, green: 0.361, blue: 1.0, alpha: 1)]  // #7c5cff
                as CFArray, locations: [0, 1])!
    let bb = mark.boundingBox
    ctx.drawLinearGradient(grad, start: CGPoint(x: bb.minX, y: bb.maxY),
                           end: CGPoint(x: bb.maxX, y: bb.minY), options: [])
    ctx.restoreGState()

    return ctx.makeImage()!
}

func writePNG(_ img: CGImage, _ path: String) {
    let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL,
                                               UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, img, nil)
    CGImageDestinationFinalize(dest)
}

let outDir = CommandLine.arguments[1]
let specs: [(String, CGFloat)] = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
for (name, sz) in specs { writePNG(drawIcon(size: sz), outDir + "/" + name) }
print("ok")
