using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;

namespace OfflineWord.Setup {
    internal static class Native {
        public static string DefaultRoot { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","OfflineWord"); } }
        public static void RequireLocal(string value) {
            foreach(char character in value) if(character<32) throw new SetupFailure("invalid_path");
            string full=Path.GetFullPath(value);
            if(full.StartsWith("\\\\") || full.Length>240 || full.Length<3 || full[1]!=':' || full.IndexOf(':',2)>=0)
                throw new SetupFailure("invalid_path");
            string drive=Path.GetPathRoot(full);
            var type=new DriveInfo(drive).DriveType;
            if(type!=DriveType.Fixed && type!=DriveType.Removable) throw new SetupFailure("invalid_path");
            for(string p=full; !String.IsNullOrEmpty(p); p=Path.GetDirectoryName(p)) {
                if((File.Exists(p)||Directory.Exists(p)) && (File.GetAttributes(p)&FileAttributes.ReparsePoint)!=0)
                    throw new SetupFailure("invalid_path");
                if(p==drive) break;
            }
        }
        public static void RequireSpace(string root,long required) {
            if(new DriveInfo(Path.GetPathRoot(root)).AvailableFreeSpace<required) throw new SetupFailure("disk_space");
            string local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if(new DriveInfo(Path.GetPathRoot(local)).AvailableFreeSpace<Math.Min(required,1536L*1024*1024)) throw new SetupFailure("disk_space");
        }
        public static string InstalledVersion(string root) {
            try {
                RequireLocal(root);
                var file=new FileInfo(Path.Combine(root,"build-info.json"));
                if(!file.Exists || file.Length>65536) return null;
                using(var input=file.OpenRead()) {
                    var values=Bundle.ReadJson<System.Collections.Generic.Dictionary<string,object>>(input);
                    object value; return values.TryGetValue("application_version",out value) ? value as string : null;
                }
            } catch(Exception) { return null; }
        }
        public static void CheckRunning(string root) {
            foreach(var process in Process.GetProcesses()) using(process) {
                try {
                    string name=process.ProcessName.ToLowerInvariant();
                    if(name!="python" && name!="pythonw" && name!="offlineword") continue;
                    string path=Path.GetFullPath(process.MainModule.FileName);
                    if(path.StartsWith(root.TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)) throw new SetupFailure("application_running");
                } catch(System.ComponentModel.Win32Exception) { throw new SetupFailure("application_running"); }
                catch(InvalidOperationException) {} catch(ArgumentException) {}
            }
        }
        public static void ValidateExistingTree(string root,CancellationToken cancellation) {
            cancellation.ThrowIfCancellationRequested();
            RequireLocal(root);
            if(!Directory.Exists(root)) return;
            foreach(string path in Directory.EnumerateFileSystemEntries(root)) {
                RequireLocal(path);
                cancellation.ThrowIfCancellationRequested();
                if(Directory.Exists(path)) ValidateExistingTree(path,cancellation);
            }
        }
        [StructLayout(LayoutKind.Sequential)] struct Overlapped {
            public UIntPtr Internal,InternalHigh;
            public uint Offset,OffsetHigh;
            public IntPtr Event;
        }
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool LockFileEx(SafeFileHandle file,uint flags,uint reserved,uint low,uint high,ref Overlapped state);
        [DllImport("kernel32.dll",SetLastError=true)] static extern bool UnlockFileEx(SafeFileHandle file,uint reserved,uint low,uint high,ref Overlapped state);
        sealed class InstallLock : IDisposable {
            FileStream file; Overlapped state;
            public InstallLock(string path) {
                RequireLocal(path); Directory.CreateDirectory(Path.GetDirectoryName(path));
                file=new FileStream(path,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.ReadWrite);
                if(!LockFileEx(file.SafeFileHandle,3,0,1,0,ref state)) { file.Dispose(); file=null; throw new SetupFailure("application_running"); }
            }
            public void Dispose() { if(file!=null) { UnlockFileEx(file.SafeFileHandle,0,1,0,ref state); file.Dispose(); file=null; } }
        }
        public static IDisposable AcquireInstallLock(string root) {
            return new InstallLock(Path.Combine(Path.GetDirectoryName(root),"."+Path.GetFileName(root)+".update.lock"));
        }
        const string PreferencesPath=@"HKEY_CURRENT_USER\Software\OfflineWord\Installer";
        public static bool ReadLowPerformance() {
            try {return Object.Equals(Registry.GetValue(PreferencesPath,"LowPerformance",0),1);}
            catch(System.Security.SecurityException){return false;}catch(UnauthorizedAccessException){return false;}catch(IOException){return false;}
        }
        public static bool SaveLowPerformance(bool enabled) {
            try {Registry.SetValue(PreferencesPath,"LowPerformance",enabled?1:0,RegistryValueKind.DWord);return true;}
            catch(System.Security.SecurityException){return false;}catch(UnauthorizedAccessException){return false;}catch(IOException){return false;}
        }
        public static bool MotionEnabled { get { return !SystemParameters.HighContrast && SystemParameters.ClientAreaAnimation; } }
        [StructLayout(LayoutKind.Sequential)] struct PowerStatus { public byte AC,Battery,Percent,Saver; public uint Seconds,FullSeconds; }
        [DllImport("kernel32.dll")] static extern bool GetSystemPowerStatus(out PowerStatus status);
        [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
        [StructLayout(LayoutKind.Sequential)] struct Margins { public int Left,Right,Top,Bottom; public Margins(int value) {Left=Right=Top=Bottom=value;} }
        [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd,int attribute,ref int value,int size);
        [DllImport("dwmapi.dll")] static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd,ref Margins margins);
        public static bool VisualEffectsEnabled {
            get {
                PowerStatus power;
                return !SystemParameters.HighContrast && GetSystemMetrics(0x1000)==0 && !(GetSystemPowerStatus(out power)&&power.Saver==1)
                    && Convert.ToInt32(Registry.GetValue(@"HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize","EnableTransparency",1))!=0;
            }
        }
        public static bool Material(Window window,bool dark,bool allowEffects) {
            IntPtr handle=new WindowInteropHelper(window).Handle;
            if(handle==IntPtr.Zero) return false;
            bool enable=allowEffects && VisualEffectsEnabled;
            int value=dark?1:0; DwmSetWindowAttribute(handle,20,ref value,4);
            value=2; DwmSetWindowAttribute(handle,33,ref value,4);
            value=enable?3:1;
            bool active=DwmSetWindowAttribute(handle,38,ref value,4)==0 && enable;
            var margins=new Margins(active?-1:0);
            active=DwmExtendFrameIntoClientArea(handle,ref margins)==0 && active;
            var source=HwndSource.FromHwnd(handle);
            if(source!=null) source.CompositionTarget.BackgroundColor=Colors.Transparent;
            return active;
        }
        public static string PickFolder(Window owner,string current) {
            var dialog=(IFileDialog)new FileOpenDialog();
            IShellItem selected=null;
            try {
                dialog.SetOptions(0x20|0x40|0x800|0x1000); // folders, filesystem, existing path/file
                dialog.SetTitle("选择已安装的离线 OCR 文件夹");
                dialog.SetOkButtonLabel("选择文件夹");
                if(dialog.Show(new WindowInteropHelper(owner).Handle)!=0) return null;
                dialog.GetResult(out selected);
                IntPtr text; selected.GetDisplayName(0x80058000,out text);
                try { return Marshal.PtrToStringUni(text); } finally {Marshal.FreeCoTaskMem(text);}
            } finally { if(selected!=null)Marshal.ReleaseComObject(selected); Marshal.ReleaseComObject(dialog); }
        }
        [ComImport,Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")] class FileOpenDialog {}
        [ComImport,Guid("42F85136-DB7E-439C-85F1-E4075D135FC8"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] interface IFileDialog {
            [PreserveSig] int Show(IntPtr parent);
            void SetFileTypes(uint count,IntPtr filters); void SetFileTypeIndex(uint index); void GetFileTypeIndex(out uint index);
            void Advise(IntPtr events,out uint cookie); void Unadvise(uint cookie);
            void SetOptions(uint options); void GetOptions(out uint options);
            void SetDefaultFolder(IShellItem item); void SetFolder(IShellItem item); void GetFolder(out IShellItem item);
            void GetCurrentSelection(out IShellItem item);
            void SetFileName([MarshalAs(UnmanagedType.LPWStr)]string name); void GetFileName(out IntPtr name);
            void SetTitle([MarshalAs(UnmanagedType.LPWStr)]string title); void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)]string text);
            void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)]string text); void GetResult(out IShellItem result);
        }
        [ComImport,Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)] interface IShellItem {
            void BindToHandler(IntPtr bind,ref Guid handler,ref Guid iid,out IntPtr result);
            void GetParent(out IShellItem parent); void GetDisplayName(uint type,out IntPtr name);
            void GetAttributes(uint mask,out uint attributes); void Compare(IShellItem other,uint hint,out int order);
        }
    }
}
