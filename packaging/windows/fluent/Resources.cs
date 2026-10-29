using System;
using System.IO;
using System.Resources;
class ResourceBuilder {
    public static void Main(string[] args) {
        using (var output = new ResourceWriter(args[0])) {
            for (int i=1; i<args.Length; i+=2) {
                output.AddResource(args[i], new MemoryStream(File.ReadAllBytes(args[i+1])), true);
            }
        }
    }
}
