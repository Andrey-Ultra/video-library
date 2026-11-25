import AppKit
let root=URL(fileURLWithPath:CommandLine.arguments[1])
let iconset=root.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at:iconset,withIntermediateDirectories:true)
for size in [16,32,128,256,512] {
    for scale in [1,2] {
        let pixels=size*scale
        let image=NSImage(size:NSSize(width:pixels,height:pixels))
        image.lockFocus()
        let rect=NSRect(x:0,y:0,width:pixels,height:pixels)
        NSColor(calibratedRed:0.10,green:0.11,blue:0.13,alpha:1).setFill()
        NSBezierPath(roundedRect:rect.insetBy(dx:Double(pixels)*0.05,dy:Double(pixels)*0.05),xRadius:Double(pixels)*0.22,yRadius:Double(pixels)*0.22).fill()
        let pink=NSColor(calibratedRed:0.96,green:0.48,blue:0.70,alpha:1)
        let cyan=NSColor(calibratedRed:0.39,green:0.85,blue:0.84,alpha:1)
        let border=NSBezierPath(roundedRect:rect.insetBy(dx:Double(pixels)*0.16,dy:Double(pixels)*0.16),xRadius:Double(pixels)*0.15,yRadius:Double(pixels)*0.15)
        border.lineWidth=Double(pixels)*0.015
        NSGraphicsContext.saveGraphicsState();border.addClip();NSGradient(starting:pink,ending:cyan)!.draw(in:rect,angle:-45);NSGraphicsContext.restoreGraphicsState()
        NSColor(calibratedRed:0.10,green:0.11,blue:0.13,alpha:1).setFill()
        NSBezierPath(roundedRect:rect.insetBy(dx:Double(pixels)*0.175,dy:Double(pixels)*0.175),xRadius:Double(pixels)*0.135,yRadius:Double(pixels)*0.135).fill()
        let font=NSFont.systemFont(ofSize:Double(pixels)*0.34,weight:.semibold)
        let text="VL" as NSString
        let attrs:[NSAttributedString.Key:Any]=[.font:font,.foregroundColor:cyan,.kern:-Double(pixels)*0.015]
        let bounds=text.size(withAttributes:attrs)
        text.draw(at:NSPoint(x:(Double(pixels)-bounds.width)/2,y:(Double(pixels)-bounds.height)/2),withAttributes:attrs)
        image.unlockFocus()
        let rep=NSBitmapImageRep(data:image.tiffRepresentation!)!
        let name="icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try rep.representation(using:.png,properties:[:])!.write(to:iconset.appendingPathComponent(name))
    }
}
let task=Process();task.executableURL=URL(fileURLWithPath:"/usr/bin/iconutil");task.arguments=["-c","icns",iconset.path,"-o",root.appendingPathComponent("AppIcon.icns").path];try task.run();task.waitUntilExit();if task.terminationStatus != 0{exit(1)}
try FileManager.default.removeItem(at:iconset)
