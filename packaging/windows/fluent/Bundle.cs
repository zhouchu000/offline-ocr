using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace OfflineWord.Setup {
    [DataContract] public class BundleInfo {
        [DataMember] public string version;
        [DataMember] public string baseVersion;
        [DataMember] public string kind;
        [DataMember] public string backendSha256;
        [DataMember] public long backendBytes;
        [DataMember] public long installedBytes;
        [DataMember] public Dictionary<string,string> light;
        [DataMember] public Dictionary<string,string> dark;
        [DataMember] public string[] changes;
    }
    public class SetupFailure : Exception {
        public readonly string Code;
        public SetupFailure(string code) : base(code) { Code=code; }
    }
    public class ProgressState {
        public readonly string Stage;
        public readonly double Fraction;
        public ProgressState(string stage, double fraction) { Stage=stage; Fraction=fraction; }
    }
    public sealed class Bundle : IDisposable {
        public readonly BundleInfo Info;
        public readonly string Executable;
        private string work;
        private long offset;
        public string StatusPath { get { return work==null ? null : Path.Combine(work,"setup-status.txt"); } }
        public Bundle(string executable) {
            Executable=Path.GetFullPath(executable);
            using (var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("setup.json")) {
                Info=ReadJson<BundleInfo>(stream);
            }
            if (Info.kind!="install" && Info.kind!="update") throw new SetupFailure("invalid_bundle");
        }
        public static T ReadJson<T>(Stream input) {
            return (T)new DataContractJsonSerializer(typeof(T), new DataContractJsonSerializerSettings {UseSimpleDictionaryFormat=true}).ReadObject(input);
        }
        public void ValidateTrailer() {
            using (var stream=File.OpenRead(Executable)) {
                if(stream.Length<32) throw new SetupFailure("damaged_package");
                stream.Seek(-24,SeekOrigin.End);
                using(var reader=new BinaryReader(stream,Encoding.ASCII,true)) {
                    if(Encoding.ASCII.GetString(reader.ReadBytes(8))!="OWSETUP1") throw new SetupFailure("damaged_package");
                    offset=reader.ReadInt64();
                    long size=reader.ReadInt64();
                    if(offset<1024 || size!=Info.backendBytes || size<2 || size>4L*1024*1024*1024 || offset!=stream.Length-size-24)
                        throw new SetupFailure("damaged_package");
                }
            }
        }
        private void CreateWork() {
            if(work!=null) return;
            string parent=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"OfflineWord","Setup");
            Native.RequireLocal(parent);
            Directory.CreateDirectory(parent);
            work=Path.Combine(parent,Guid.NewGuid().ToString("N"));
            var acl=new DirectorySecurity();
            acl.SetAccessRuleProtection(true,false);
            foreach(var sid in new[]{WindowsIdentity.GetCurrent().User,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)})
                acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            Directory.CreateDirectory(work,acl);
        }
        public Task<string> Prepare(IProgress<ProgressState> progress, CancellationToken cancellation) {
            return Task.Run(() => {
                ValidateTrailer(); CreateWork();
                string backend=Path.Combine(work,"backend.exe");
                using(var input=File.OpenRead(Executable))
                using(var output=new FileStream(backend,FileMode.Create,FileAccess.Write,FileShare.None,1024*1024))
                using(var hash=SHA256.Create()) {
                    input.Position=offset;
                    byte[] buffer=new byte[1024*1024];
                    long copied=0; var clock=Stopwatch.StartNew();
                    while(copied<Info.backendBytes) {
                        cancellation.ThrowIfCancellationRequested();
                        int count=input.Read(buffer,0,(int)Math.Min(buffer.Length,Info.backendBytes-copied));
                        if(count==0) throw new SetupFailure("damaged_package");
                        output.Write(buffer,0,count); hash.TransformBlock(buffer,0,count,null,0); copied+=count;
                        if(clock.ElapsedMilliseconds>=50) { progress.Report(new ProgressState("preparing",(double)copied/Info.backendBytes)); clock.Restart(); }
                    }
                    hash.TransformFinalBlock(new byte[0],0,0); output.Flush(true);
                    if(Hex(hash.Hash)!=Info.backendSha256) throw new SetupFailure("damaged_package");
                }
                progress.Report(new ProgressState("preparing",1));
                return backend;
            },cancellation);
        }
        public async Task Execute(string root, bool rollback, IProgress<ProgressState> progress, CancellationToken cancellation, Action writing) {
            var lease=await Task.Run(() => {
                Native.RequireLocal(root);
                long space=Info.kind=="install" ? Info.installedBytes+Info.backendBytes+256L*1024*1024 : Info.backendBytes+256L*1024*1024;
                Native.RequireSpace(root,space);
                var acquired=Info.kind=="install" ? Native.AcquireInstallLock(root) : null;
                try {
                    Native.CheckRunning(root);
                    if(Info.kind=="install") Native.ValidateExistingTree(root,cancellation);
                    cancellation.ThrowIfCancellationRequested();return acquired;
                }catch {if(acquired!=null)acquired.Dispose();throw;}
            },cancellation);
            using(lease) {
                string backend=await Prepare(progress,cancellation);
                cancellation.ThrowIfCancellationRequested();
                await Task.Run(() => {
                    Native.CheckRunning(root);
                    if(Info.kind=="install") Native.ValidateExistingTree(root,cancellation);
                },cancellation);
                cancellation.ThrowIfCancellationRequested();
                writing(); progress.Report(new ProgressState("installing",-1));
                if(File.Exists(StatusPath)) File.Delete(StatusPath);
                int code=await Task.Run(() => {
                    string arguments="/S"+(rollback?" /ROLLBACK":"");
                    if(Info.kind=="update") arguments+=" /D="+root;
                    using(var child=Process.Start(new ProcessStartInfo(backend,arguments) {UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=work})) {
                        child.WaitForExit(); return child.ExitCode;
                    }
                });
                if(code!=0) throw new SetupFailure("backend_failed");
                if(Info.kind=="install") await VerifyInstalled(root,progress);
                else {
                    string expected=rollback?Info.baseVersion:Info.version;
                    if(Native.InstalledVersion(root)!=expected) throw new SetupFailure("verification_failed");
                }
            }
        }
        public Task VerifyInstalled(string root,IProgress<ProgressState> progress) {
            return Task.Run(() => {
                Dictionary<string,string> files;
                using(var index=Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.json")) files=ReadJson<Dictionary<string,string>>(index);
                long completed=0; byte[] buffer=new byte[1024*1024]; var clock=Stopwatch.StartNew();
                foreach(var pair in files) {
                    string path=Path.GetFullPath(Path.Combine(root,pair.Key.Replace('/',Path.DirectorySeparatorChar)));
                    if(!path.StartsWith(root.TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)) throw new SetupFailure("verification_failed");
                    Native.RequireLocal(path);
                    using(var input=File.OpenRead(path)) using(var hash=SHA256.Create()) {
                        int count;
                        while((count=input.Read(buffer,0,buffer.Length))>0) {
                            hash.TransformBlock(buffer,0,count,null,0); completed+=count;
                            if(clock.ElapsedMilliseconds>=50) { progress.Report(new ProgressState("verifying",Math.Min(1,(double)completed/Info.installedBytes))); clock.Restart(); }
                        }
                        hash.TransformFinalBlock(new byte[0],0,0);
                        if(Hex(hash.Hash)!=pair.Value) throw new SetupFailure("verification_failed");
                    }
                }
                if(Native.InstalledVersion(root)!=Info.version) throw new SetupFailure("verification_failed");
                progress.Report(new ProgressState("verifying",1));
            });
        }
        public static string Hex(byte[] data) { return BitConverter.ToString(data).Replace("-","").ToLowerInvariant(); }
        public void Dispose() {
            if(work==null) return;
            // Delete only the private directory created by this instance; never follow links.
            try { Native.ValidateExistingTree(work,CancellationToken.None); Directory.Delete(work,true); }
            catch(IOException) {} catch(UnauthorizedAccessException) {} catch(SetupFailure) {}
            work=null;
        }
    }
}
