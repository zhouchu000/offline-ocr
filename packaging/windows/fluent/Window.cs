using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shell;
using System.Windows.Threading;
using Microsoft.Win32;

namespace OfflineWord.Setup {
    public sealed partial class SetupWindow : Window {
        readonly Bundle bundle;
        readonly Grid view;
        readonly DispatcherTimer statusTimer;
        CancellationTokenSource cancellation;
        string root;
        string state="ready";
        bool lowPerformance,updatingModeControl,rollback,busy,writing,closeWhenSafe,showingNotes;
#if QA
        bool qaPermitInstall;
#endif
        public bool GlassActive {get;private set;}
        bool MotionAllowed {get{return !lowPerformance && Native.MotionEnabled;}}
        T Control<T>(string name) where T:FrameworkElement {return (T)view.FindName(name);}
        TextBlock Label(string name) {return Control<TextBlock>(name);}
        Button Button(string name) {return Control<Button>(name);}
        public SetupWindow(Bundle value,string installationRoot,bool restore=false) {
            bundle=value; root=installationRoot; rollback=restore;
            Title="离线 OCR "+bundle.Info.version+(bundle.Info.kind=="install"?" 安装":" 更新");
            Width=Math.Min(760,SystemParameters.WorkArea.Width-32); Height=Math.Min(540,SystemParameters.WorkArea.Height-32);
            MinWidth=Math.Min(660,Width); MinHeight=Math.Min(460,Height);
            ResizeMode=ResizeMode.CanMinimize; WindowStyle=WindowStyle.None; WindowStartupLocation=WindowStartupLocation.CenterScreen;
            Background=Brushes.Transparent;
            WindowChrome.SetWindowChrome(this,new WindowChrome {CaptionHeight=0,ResizeBorderThickness=new Thickness(0),GlassFrameThickness=new Thickness(-1),CornerRadius=new CornerRadius(10),UseAeroCaptionButtons=false});
            using(var input=Assembly.GetExecutingAssembly().GetManifestResourceStream("window.xaml")) view=(Grid)XamlReader.Load(input);
            Content=view; FontSize=14;
            FontFamily=new FontFamily(new Uri("pack://application:,,,/FluentInstaller;component/"),"./assets/#MiSans");
            TextOptions.SetTextFormattingMode(this,TextFormattingMode.Display);
            TextOptions.SetTextRenderingMode(this,TextRenderingMode.ClearType);
            UseLayoutRounding=true; SnapsToDevicePixels=true;
            Control<Grid>("TitleBar").MouseLeftButtonDown+=(s,e)=> {
                var item=e.OriginalSource as DependencyObject;
                while(item!=null) {if(item is ButtonBase)return;item=VisualTreeHelper.GetParent(item);}
                if(e.ChangedButton==MouseButton.Left)try{DragMove();}catch(InvalidOperationException){}
            };
            Button("MinButton").Click+=(s,e)=>WindowState=WindowState.Minimized;
            Button("CloseButton").Click+=(s,e)=>Close();
            Button("SettingsButton").Click+=(s,e)=>ShowSettings(Control<Grid>("SettingsLayer").Visibility!=Visibility.Visible);
            Button("SettingsCloseButton").Click+=(s,e)=>ShowSettings(false);
            Control<Grid>("SettingsLayer").MouseDown+=(s,e)=>{if(e.OriginalSource==s)ShowSettings(false);};
            PreviewKeyDown+=(s,e)=>{if(e.Key==Key.Escape&&Control<Grid>("SettingsLayer").Visibility==Visibility.Visible){ShowSettings(false);e.Handled=true;}};
            RoutedEventHandler modeChanged=(s,e)=> {
                if(updatingModeControl)return;
                lowPerformance=Control<ToggleButton>("LowPerformanceToggle").IsChecked==true;ApplyTheme();
#if !QA
                if(!Native.SaveLowPerformance(lowPerformance))Label("Notice").Text="设置仅在本次安装中生效。";
#endif
            };
            Control<ToggleButton>("LowPerformanceToggle").Checked+=modeChanged;
            Control<ToggleButton>("LowPerformanceToggle").Unchecked+=modeChanged;
            Button("PrimaryButton").Click+=async(s,e)=> {
#if QA
                if(!qaPermitInstall)return;
#endif
                if(state=="success") OpenApplication();
                else if(!busy) await RunOperation();
            };
            Button("SecondaryButton").Click+=(s,e)=> {
                if(busy&&!writing) {closeWhenSafe=false; cancellation.Cancel();}
                else if(!busy) Close();
            };
            Button("DetailsButton").Click+=(s,e)=>ToggleNotes();
            Button("RollbackButton").Click+=(s,e)=> {rollback=!rollback;Ready();AnimatePage();};
            Button("BrowseButton").Click+=(s,e)=> {
                try {string chosen=Native.PickFolder(this,root); if(chosen!=null){Native.RequireLocal(chosen);root=Path.GetFullPath(chosen);Ready();}}
                catch(Exception){ShowError("invalid_path");}
            };
            Closing+=OnClosing;
            SourceInitialized+=(s,e)=>ApplyTheme();
            Loaded+=(s,e)=>{Ready();AnimatePage();Button("PrimaryButton").Focus();};
            StateChanged+=(s,e)=>{
                if(WindowState==WindowState.Minimized) {StopMotion();Control<ProgressBar>("Progress").IsIndeterminate=false;}
                else if(busy&&state=="installing") Control<ProgressBar>("Progress").IsIndeterminate=MotionAllowed;
            };
            SystemEvents.UserPreferenceChanged+=PreferencesChanged;
            statusTimer=new DispatcherTimer {Interval=TimeSpan.FromMilliseconds(300)};
            statusTimer.Tick+=(s,e)=>ReadStatus();
            Closed+=(s,e)=>{statusTimer.Stop();SystemEvents.UserPreferenceChanged-=PreferencesChanged;bundle.Dispose();};
#if !QA
            lowPerformance=Native.ReadLowPerformance();
#endif
            ApplyTheme();
        }
        void PreferencesChanged(object sender,UserPreferenceChangedEventArgs e) {
            Dispatcher.BeginInvoke(new Action(ApplyTheme));
        }
        void ShowSettings(bool show) {
            Control<Grid>("SettingsLayer").Visibility=show?Visibility.Visible:Visibility.Collapsed;
            Control<Grid>("Page").IsEnabled=!show;
            Button("DetailsButton").IsEnabled=!show&&!busy;
            Button("PrimaryButton").IsEnabled=!show&&!busy;
            Button("SecondaryButton").IsEnabled=!show&&(!busy||!writing);
            if(show)Control<ToggleButton>("LowPerformanceToggle").Focus();
            else Button("SettingsButton").Focus();
        }
        void ApplyTheme() {
            bool dark=!lowPerformance;
            var palette=dark?bundle.Info.dark:bundle.Info.light;
            foreach(var pair in palette) if(pair.Value.StartsWith("#")) Resources[pair.Key]=new SolidColorBrush((Color)ColorConverter.ConvertFromString(pair.Value));
            GlassActive=Native.Material(this,dark,!lowPerformance);
            Control<Grid>("LightEffects").Visibility=!lowPerformance&&Native.VisualEffectsEnabled?Visibility.Visible:Visibility.Collapsed;
            Color background=(Color)ColorConverter.ConvertFromString(palette["window"]);
            background.A=GlassActive?(byte)(dark?195:208):(byte)255;
            Control<Border>("MaterialRoot").Background=new SolidColorBrush(background);
            Color panel=(Color)ColorConverter.ConvertFromString(palette["surface"]); panel.A=GlassActive?(byte)155:(byte)255;
            Resources["panel"]=new SolidColorBrush(panel);
            if(SystemParameters.HighContrast) {
                Resources["text"]=SystemColors.WindowTextBrush; Resources["secondary"]=SystemColors.WindowTextBrush; Resources["muted"]=SystemColors.WindowTextBrush;
                Resources["window"]=SystemColors.WindowBrush; Resources["surface"]=SystemColors.WindowBrush; Resources["panel"]=SystemColors.WindowBrush;
                Resources["primary"]=SystemColors.HighlightBrush; Resources["on_primary"]=SystemColors.HighlightTextBrush; Resources["border"]=SystemColors.WindowTextBrush;
                Control<Border>("MaterialRoot").Background=SystemColors.WindowBrush;
            }
            updatingModeControl=true;
            try{Control<ToggleButton>("LowPerformanceToggle").IsChecked=lowPerformance;}finally{updatingModeControl=false;}
            if(!MotionAllowed) {StopMotion();Control<ProgressBar>("Progress").IsIndeterminate=false;}
            else if(busy&&state=="installing"&&WindowState!=WindowState.Minimized)Control<ProgressBar>("Progress").IsIndeterminate=true;
        }
        void Ready() {
            state="ready"; busy=writing=false; showingNotes=false;
            bool update=bundle.Info.kind=="update";
            Label("Eyebrow").Text=rollback?"恢复版本":update?"更新应用":"安装应用";
            Label("Heading").Text=rollback?"恢复原版本":update?"更新离线 OCR":"安装离线 OCR";
            Label("Description").Text=rollback?"恢复程序文件，保留图片与文档。":update?"离线更新，保留图片与文档。":"本地识别、校对与导出。";
            Label("VersionLabel").Text=rollback?bundle.Info.baseVersion:bundle.Info.version;
            Label("LocationLabel").Text=root;
            Label("Notice").Text=update?"请先关闭离线 OCR，更新后保留原版本备份。":"安装到当前用户，无需联网下载。";
            Control<Border>("InfoPanel").Visibility=Visibility.Visible;
            Control<StackPanel>("NotesPanel").Visibility=Visibility.Collapsed;
            Control<StackPanel>("ProgressPanel").Visibility=Visibility.Collapsed;
            Button("RollbackButton").Visibility=update?Visibility.Visible:Visibility.Collapsed;
            Button("RollbackButton").Content=rollback?"返回更新":"恢复更新前版本";
            Button("BrowseButton").Visibility=update?Visibility.Visible:Visibility.Collapsed;
            Button("PrimaryButton").Content=rollback?"恢复版本":update?"开始更新":"开始安装";
            Button("SecondaryButton").Content="取消";
            Button("DetailsButton").Content="本次更新";
            SetButtons(true);
        }
        void SetButtons(bool idle) {
            bool settings=Control<Grid>("SettingsLayer").Visibility==Visibility.Visible;
            Button("PrimaryButton").IsEnabled=idle&&!settings;
            Button("SecondaryButton").IsEnabled=(idle||!writing)&&!settings;
            Button("DetailsButton").IsEnabled=idle&&!settings;
            Button("BrowseButton").IsEnabled=idle;
            Button("RollbackButton").IsEnabled=idle;
        }
        async Task RunOperation() {
            ShowSettings(false); Ready(); busy=true; state="preparing";
            cancellation=new CancellationTokenSource();
            Control<Border>("InfoPanel").Visibility=Visibility.Collapsed;
            Button("RollbackButton").Visibility=Visibility.Collapsed;
            Control<StackPanel>("ProgressPanel").Visibility=Visibility.Visible;
            Label("Heading").Text=rollback?"正在恢复原版本。":bundle.Info.kind=="update"?"正在更新离线 OCR。":"正在安装离线 OCR。";
            Label("Description").Text="正在处理程序文件。";
            Label("Notice").Text="";
            Button("PrimaryButton").Content="请稍候"; SetButtons(false); AnimatePage();
            var progress=new Progress<ProgressState>(ShowProgress);
            try {
                ((IProgress<ProgressState>)progress).Report(new ProgressState("preparing",0));
                await bundle.Execute(root,rollback,progress,cancellation.Token,()=> {writing=true;SetButtons(false);statusTimer.Start();});
                statusTimer.Stop();busy=writing=false;state="success";
                ShowSuccess();
            } catch(OperationCanceledException) {
                statusTimer.Stop();busy=writing=false;Ready();Label("Notice").Text="已取消，程序文件尚未改动。";
                if(closeWhenSafe) Close();
            } catch(Exception error) {
                statusTimer.Stop();busy=writing=false;
                var known=error as SetupFailure;
                ShowError(known!=null?known.Code:"io_failed");
            } finally { cancellation.Dispose();cancellation=null; }
        }
        void ShowProgress(ProgressState progress) {
            state=progress.Stage;
            Label("StageLabel").Text=state=="preparing"?"准备并核对安装文件":state=="verifying"?"核对安装结果":rollback?"恢复原版本":bundle.Info.kind=="update"?"核对版本并更新":"写入程序文件";
            var bar=Control<ProgressBar>("Progress");
            bar.IsIndeterminate=progress.Fraction<0&&MotionAllowed&&WindowState!=WindowState.Minimized;
            bar.Value=progress.Fraction<0?0:progress.Fraction*100;
            Label("PercentLabel").Text=progress.Fraction<0?"进行中":Math.Round(progress.Fraction*100)+"%";
            Label("ProgressHint").Text=writing?"完成前请保持此窗口打开。":"此阶段可以安全取消。";
        }
        void ReadStatus() {
            try {
                string path=bundle.StatusPath;
                if(path==null||!File.Exists(path)||new FileInfo(path).Length>4096) return;
                using(var input=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite))
                using(var reader=new StreamReader(input,Encoding.Unicode)) {
                    string message=reader.ReadToEnd().Trim();
                    if(message=="registering") Label("StageLabel").Text="创建应用入口";
                    else if(message=="extracting") Label("StageLabel").Text="写入程序文件";
                }
            }catch(IOException){}catch(UnauthorizedAccessException){}
        }
        void ShowSuccess() {
            Control<ProgressBar>("Progress").IsIndeterminate=false;
            Control<StackPanel>("ProgressPanel").Visibility=Visibility.Collapsed;
            Control<Border>("InfoPanel").Visibility=Visibility.Visible;
            Label("Eyebrow").Text="已完成";
            Label("Heading").Text=rollback?"恢复完成":"操作完成";
            Label("Description").Text=rollback?"已恢复原版本":"离线 OCR "+bundle.Info.version+" 已安装。";
            Label("VersionLabel").Text=rollback?bundle.Info.baseVersion:bundle.Info.version;
            Label("Notice").Text="可打开应用。";
            Button("PrimaryButton").Content="打开应用";Button("SecondaryButton").Content="完成";
            SetButtons(true);Button("RollbackButton").Visibility=Visibility.Collapsed;Button("BrowseButton").Visibility=Visibility.Collapsed;AnimatePage();
        }
        void ShowError(string code) {
            state="error";Control<ProgressBar>("Progress").IsIndeterminate=false;
            Control<StackPanel>("ProgressPanel").Visibility=Visibility.Collapsed;
            Control<Border>("InfoPanel").Visibility=Visibility.Visible;
            Label("Eyebrow").Text="尚未完成";Label("Heading").Text="安装未能完成。";
            string message=code=="application_running"?"请关闭离线 OCR 和识别任务，再重试。":code=="disk_space"?"可用磁盘空间不足，请腾出空间后重试。":code=="damaged_package"?"安装包不完整，请重新复制完整安装包。":code=="invalid_path"?"请选择本机的离线 OCR 安装目录。":code=="verification_failed"?"安装结果未通过校验，请关闭应用后重试。":"未能完成操作，请确认版本匹配、目录可写，再重试。";
            if(code=="backend_failed"&&bundle.Info.kind=="update") {
                try {
                    string path=bundle.StatusPath;
                    if(path!=null&&File.Exists(path)&&new FileInfo(path).Length<4096) {
                        string status=File.ReadAllText(path,Encoding.Unicode).Trim();
                        int detail=status.IndexOf('（');
                        if(detail>0&&detail<300) message=status.Substring(0,detail);
                    }
                }catch(IOException){}catch(UnauthorizedAccessException){}
            }
            Label("Description").Text=message;
            Label("Notice").Text=code=="backend_failed"&&bundle.Info.kind=="update"?"保留更新备份，重新运行可继续恢复。":"";
            Button("PrimaryButton").Content="重试";Button("SecondaryButton").Content="关闭";SetButtons(true);AnimatePage();
        }
        void OpenApplication() {
            try {
                Native.RequireLocal(root);
                Process.Start(new ProcessStartInfo(Path.Combine(root,"runtime","pythonw.exe"),"-B \""+Path.Combine(root,"launch.pyw")+"\"") {UseShellExecute=false,WorkingDirectory=root});
                Close();
            }catch(Exception){Label("Notice").Text="应用未能启动，请从桌面快捷方式打开。";}
        }
        void ToggleNotes() {
            showingNotes=!showingNotes;
            var notes=Control<StackPanel>("NotesPanel");notes.Children.Clear();
            foreach(string change in bundle.Info.changes) notes.Children.Add(new TextBlock {Text=change,FontSize=13,Margin=new Thickness(0,0,0,12),TextWrapping=TextWrapping.Wrap});
            notes.Visibility=showingNotes?Visibility.Visible:Visibility.Collapsed;
            Control<Border>("InfoPanel").Visibility=showingNotes?Visibility.Collapsed:Visibility.Visible;
            Button("RollbackButton").Visibility=!showingNotes&&bundle.Info.kind=="update"&&state=="ready"?Visibility.Visible:Visibility.Collapsed;
            Button("DetailsButton").Content=showingNotes?"返回":"本次更新";
            AnimatePage();
        }
        void OnClosing(object sender,CancelEventArgs e) {
            if(!busy) return;
            e.Cancel=true;
            if(!writing) {closeWhenSafe=true;cancellation.Cancel();}
            else Label("ProgressHint").Text="正在完成安装，请稍候再关闭。";
        }
        void AnimatePage() {
            StopMotion();if(!MotionAllowed||WindowState==WindowState.Minimized)return;
            var page=Control<Grid>("Page");
            var ease=new CubicEase {EasingMode=EasingMode.EaseOut};
            page.BeginAnimation(OpacityProperty,new DoubleAnimation(.25,1,TimeSpan.FromMilliseconds(240)){EasingFunction=ease});
            page.RenderTransform.BeginAnimation(TranslateTransform.YProperty,new DoubleAnimation(10,0,TimeSpan.FromMilliseconds(280)){EasingFunction=ease});
        }
        void StopMotion() {
            var page=Control<Grid>("Page");page.BeginAnimation(OpacityProperty,null);page.Opacity=1;
            page.RenderTransform.BeginAnimation(TranslateTransform.YProperty,null);((TranslateTransform)page.RenderTransform).Y=0;
        }
    }
    internal static class Program {
        [STAThread] public static int Main(string[] args) {
            bool quiet=Array.Exists(args,arg=>arg.Equals("/S",StringComparison.OrdinalIgnoreCase));
            try {
                var bundle=new Bundle(Assembly.GetExecutingAssembly().Location);
#if QA
                if(args.Length==2&&args[0]=="--qa-protocol") return ProtocolTests.Run(bundle,args[1]);
#endif
                string root=Native.DefaultRoot;bool silent=false,rollback=false,qaMode=false;
#if QA
                qaMode=(args.Length==2&&args[0]=="--qa-output")||(args.Length==3&&args[0]=="--qa-install");
#endif
                for(int i=0;!qaMode&&i<args.Length;i++) {
                    if(args[i].Equals("/S",StringComparison.OrdinalIgnoreCase)){silent=true;continue;}
                    if(args[i].Equals("/ROLLBACK",StringComparison.OrdinalIgnoreCase)){rollback=true;continue;}
                    if(bundle.Info.kind=="install"&&args[i].StartsWith("/D=",StringComparison.OrdinalIgnoreCase)) throw new SetupFailure("unsupported_destination");
                    if(bundle.Info.kind=="update"&&args[i].StartsWith("/D=",StringComparison.OrdinalIgnoreCase)) {root=args[i].Substring(3); for(int j=i+1;j<args.Length;j++)root+=" "+args[j];break;}
                    throw new SetupFailure("unsupported_operation");
                }
                if(rollback&&bundle.Info.kind!="update") throw new SetupFailure("unsupported_operation");
#if QA
                if(args.Length==3&&args[0]=="--qa-install") root=Path.GetFullPath(args[1]);
#endif
                Native.RequireLocal(root);
                if(silent) {
                    using(bundle) bundle.Execute(root,rollback,new Progress<ProgressState>(),CancellationToken.None,()=>{}).GetAwaiter().GetResult();
                    return 0;
                }
                var app=new Application {ShutdownMode=ShutdownMode.OnMainWindowClose};
                var window=new SetupWindow(bundle,root,rollback);
#if QA
                if(args.Length==2&&args[0]=="--qa-output") window.StartQa(args[1]);
                if(args.Length==3&&args[0]=="--qa-install") window.StartInstallQa(args[2]);
#endif
                return app.Run(window);
            }catch(Exception error) {
#if QA
                if(args.Length>=2&&args[0].StartsWith("--qa-")) {
                    string folder=args[args.Length-1];Directory.CreateDirectory(folder);File.WriteAllText(Path.Combine(folder,"startup-error.txt"),error.ToString());
                }
#endif
                if(!quiet) MessageBox.Show(error is SetupFailure?"无法处理所选安装参数，请正常打开安装器后操作。":"安装器未能启动。请确认文件完整，并使用 Windows 10/11 64 位系统。","离线 OCR",MessageBoxButton.OK,MessageBoxImage.Information);
                return 1;
            }
        }
    }
}
