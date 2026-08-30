#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <netinet/in.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

static NSString *const ProviderID = @"volcengine_agent_plan";
static NSString *const ModelID = @"doubao-seed-2.1-turbo";
static NSString *const Host = @"127.0.0.1";
static const uint16_t Port = 8000;

typedef NS_ENUM(NSInteger, LauncherLogLevel) {
    LauncherLogDebug = 10,
    LauncherLogInfo = 20,
    LauncherLogWarning = 30,
    LauncherLogError = 40,
    LauncherLogOff = 100,
};

static NSString *LevelLabel(LauncherLogLevel level) {
    switch (level) {
        case LauncherLogDebug:
            return @"DEBUG";
        case LauncherLogInfo:
            return @"INFO";
        case LauncherLogWarning:
            return @"WARNING";
        case LauncherLogError:
            return @"ERROR";
        case LauncherLogOff:
            return @"OFF";
    }
}

static LauncherLogLevel ConfiguredLevel(NSString *module) {
    NSString *name = [NSString stringWithFormat:@"SEARCH_LOG_LEVEL_%@", module.uppercaseString];
    const char *rawValue = getenv(name.UTF8String);
    if (rawValue == NULL) {
        return LauncherLogInfo;
    }
    NSString *value = [NSString stringWithUTF8String:rawValue].uppercaseString;
    if ([value isEqualToString:@"DEBUG"]) {
        return LauncherLogDebug;
    }
    if ([value isEqualToString:@"WARNING"]) {
        return LauncherLogWarning;
    }
    if ([value isEqualToString:@"ERROR"] || [value isEqualToString:@"CRITICAL"]) {
        return LauncherLogError;
    }
    if ([value isEqualToString:@"OFF"]) {
        return LauncherLogOff;
    }
    return LauncherLogInfo;
}

static void EmitLog(
    NSString *traceID,
    NSString *module,
    LauncherLogLevel level,
    NSString *event,
    NSDictionary<NSString *, id> *safeContext
) {
    if (level < ConfiguredLevel(module)) {
        return;
    }
    NSISO8601DateFormatter *formatter = [[NSISO8601DateFormatter alloc] init];
    formatter.formatOptions = NSISO8601DateFormatWithInternetDateTime |
        NSISO8601DateFormatWithFractionalSeconds;
    NSMutableDictionary<NSString *, id> *record = [@{
        @"event": event,
        @"level": LevelLabel(level),
        @"logger": [@"search_quality." stringByAppendingString:module],
        @"module": module,
        @"timestamp_utc": [formatter stringFromDate:[NSDate date]],
        @"trace_id": traceID,
    } mutableCopy];
    [record addEntriesFromDictionary:safeContext];
    if (![NSJSONSerialization isValidJSONObject:record]) {
        return;
    }
    NSData *data = [NSJSONSerialization dataWithJSONObject:record options:0 error:nil];
    if (data == nil) {
        return;
    }
    fwrite(data.bytes, 1, data.length, stderr);
    fputc('\n', stderr);
    fflush(stderr);
}

static BOOL ConfigureProcessSafety(void) {
    umask(0077);
    struct rlimit coreLimit = {0, 0};
    return setrlimit(RLIMIT_CORE, &coreLimit) == 0;
}

static BOOL RequirePortAvailable(void) {
    int descriptor = socket(AF_INET, SOCK_STREAM, 0);
    if (descriptor < 0) {
        return NO;
    }
    struct sockaddr_in address = {0};
    address.sin_len = sizeof(address);
    address.sin_family = AF_INET;
    address.sin_port = htons(Port);
    address.sin_addr.s_addr = inet_addr(Host.UTF8String);
    int result = bind(
        descriptor,
        (const struct sockaddr *)&address,
        sizeof(address)
    );
    close(descriptor);
    return result == 0;
}

static NSString *CreatePrivateArtifactRoot(void) {
    char pathTemplate[] = "/private/tmp/search-agent-volcengine.XXXXXX";
    char *created = mkdtemp(pathTemplate);
    if (created == NULL || chmod(created, 0700) != 0) {
        if (created != NULL) {
            rmdir(created);
        }
        return nil;
    }
    return [NSString stringWithUTF8String:created];
}

static void RemoveEmptyArtifactRoot(NSString *path) {
    if ([path hasPrefix:@"/private/tmp/search-agent-volcengine."]) {
        rmdir(path.fileSystemRepresentation);
    }
}

static BOOL ValidateKey(NSString *candidate) {
    NSData *encoded = [candidate dataUsingEncoding:NSUTF8StringEncoding];
    if (encoded == nil || encoded.length < 24 || encoded.length > 200) {
        return NO;
    }
    const unsigned char *bytes = encoded.bytes;
    if (
        bytes[0] != 'a' || bytes[1] != 'r' || bytes[2] != 'k' || bytes[3] != '-'
    ) {
        return NO;
    }
    for (NSUInteger index = 4; index < encoded.length; index++) {
        unsigned char value = bytes[index];
        BOOL allowed =
            (value >= 'A' && value <= 'Z') ||
            (value >= 'a' && value <= 'z') ||
            (value >= '0' && value <= '9') ||
            value == '.' || value == '_' || value == '-';
        if (!allowed) {
            return NO;
        }
    }
    return YES;
}

static NSString *PromptForKey(NSString *traceID) {
    NSApplication *application = NSApplication.sharedApplication;
    [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [application activateIgnoringOtherApps:YES];

    for (NSInteger attempt = 1; attempt <= 3; attempt++) {
        EmitLog(
            traceID,
            @"launcher_dialog",
            LauncherLogInfo,
            @"launcher_dialog_opened",
            @{@"attempt": @(attempt)}
        );
        NSSecureTextField *field = [[NSSecureTextField alloc]
            initWithFrame:NSMakeRect(0, 0, 430, 26)];
        field.placeholderString = @"ark-…";
        field.usesSingleLineMode = YES;
        field.editable = YES;
        field.selectable = YES;

        NSAlert *alert = [[NSAlert alloc] init];
        alert.alertStyle = NSAlertStyleInformational;
        alert.messageText = @"搜索评测 Agent · 火山 Agent Plan";
        alert.informativeText = @"Key 只进入本机后端进程内存，不经过聊天、Shell、剪贴板读取、浏览器、文件或日志。";
        alert.accessoryView = field;
        [alert addButtonWithTitle:@"启动本地后端"];
        [alert addButtonWithTitle:@"取消"];
        alert.window.initialFirstResponder = field;

        NSModalResponse response = [alert runModal];
        if (response != NSAlertFirstButtonReturn) {
            field.stringValue = @"";
            EmitLog(
                traceID,
                @"launcher_dialog",
                LauncherLogInfo,
                @"launcher_dialog_cancelled",
                @{@"attempt": @(attempt)}
            );
            return nil;
        }
        NSString *candidate = [field.stringValue copy];
        field.stringValue = @"";
        if (ValidateKey(candidate)) {
            EmitLog(
                traceID,
                @"launcher_dialog",
                LauncherLogInfo,
                @"launcher_key_accepted",
                @{@"attempt": @(attempt)}
            );
            return candidate;
        }
        candidate = @"";
        EmitLog(
            traceID,
            @"launcher_dialog",
            LauncherLogWarning,
            @"launcher_key_format_rejected",
            @{@"attempt": @(attempt)}
        );
        NSAlert *invalid = [[NSAlert alloc] init];
        invalid.alertStyle = NSAlertStyleWarning;
        invalid.messageText = @"Key 格式无效";
        invalid.informativeText = @"请输入火山控制台生成、以 ark- 开头的 Agent Plan API Key。";
        [invalid addButtonWithTitle:@"重新输入"];
        [invalid runModal];
    }
    return nil;
}

static void ShowFailure(void) {
    NSApplication *application = NSApplication.sharedApplication;
    [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
    [application activateIgnoringOtherApps:YES];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.alertStyle = NSAlertStyleWarning;
    alert.messageText = @"本地后端未启动";
    alert.informativeText = @"请查看终端中的稳定错误码；诊断不会包含 API Key。";
    [alert addButtonWithTitle:@"知道了"];
    [alert runModal];
}

static char **CopyStrings(NSArray<NSString *> *strings) {
    char **pointers = calloc(strings.count + 1, sizeof(char *));
    if (pointers == NULL) {
        return NULL;
    }
    for (NSUInteger index = 0; index < strings.count; index++) {
        pointers[index] = strdup(strings[index].UTF8String);
        if (pointers[index] == NULL) {
            for (NSUInteger previous = 0; previous < index; previous++) {
                free(pointers[previous]);
            }
            free(pointers);
            return NULL;
        }
    }
    return pointers;
}

static void SecureZero(void *buffer, size_t length) {
    volatile unsigned char *bytes = buffer;
    while (length > 0) {
        *bytes = 0;
        bytes++;
        length--;
    }
}

static void ZeroAndFreeStrings(char **pointers) {
    if (pointers == NULL) {
        return;
    }
    for (NSUInteger index = 0; pointers[index] != NULL; index++) {
        SecureZero(pointers[index], strlen(pointers[index]));
        free(pointers[index]);
    }
    free(pointers);
}

static BOOL ExecBackend(
    NSString *repositoryRoot,
    NSString *pythonPath,
    NSString *catalogPath,
    NSString *artifactRoot,
    NSString *apiKey
) {
    NSArray<NSString *> *arguments = @[
        pythonPath,
        @"-m",
        @"uvicorn",
        @"apps.api.main:app",
        @"--host",
        Host,
        @"--port",
        [NSString stringWithFormat:@"%u", Port],
        @"--no-access-log",
    ];
    NSArray<NSString *> *environment = @[
        @"PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        @"LANG=en_US.UTF-8",
        @"TMPDIR=/private/tmp",
        @"PYTHONDONTWRITEBYTECODE=1",
        @"PYTHONUNBUFFERED=1",
        @"SEARCH_BACKEND=local",
        [@"SEARCH_CATALOG_INDEX=" stringByAppendingString:catalogPath],
        [@"SEARCH_AGENT_ARTIFACT_ROOT=" stringByAppendingString:artifactRoot],
        @"SEARCH_AGENT_PLANNER=llm",
        [@"SEARCH_LLM_PROVIDER=" stringByAppendingString:ProviderID],
        [@"SEARCH_LLM_MODEL=" stringByAppendingString:ModelID],
        @"SEARCH_LLM_TIMEOUT_MS=30000",
        @"SEARCH_LLM_MAX_OUTPUT_TOKENS=128",
        [@"SEARCH_VOLCENGINE_AGENT_PLAN_API_KEY=" stringByAppendingString:apiKey],
        @"SEARCH_LOG_FORMAT=json",
        @"SEARCH_LOG_LEVEL=WARNING",
        @"SEARCH_LOG_LEVEL_API=INFO",
        @"SEARCH_LOG_LEVEL_AGENT_MODEL=INFO",
        @"SEARCH_LOG_LEVEL_AGENT_PROVIDER=INFO",
        @"SEARCH_LOG_LEVEL_AGENT_RUNTIME=INFO",
        @"SEARCH_LOG_LEVEL_AGENT_TOOLS=INFO",
        @"SEARCH_LOG_LEVEL_RETRIEVAL_ANALYSIS=INFO",
    ];
    if (![[NSFileManager defaultManager] changeCurrentDirectoryPath:repositoryRoot]) {
        return NO;
    }
    char **argumentPointers = CopyStrings(arguments);
    char **environmentPointers = CopyStrings(environment);
    if (argumentPointers == NULL || environmentPointers == NULL) {
        ZeroAndFreeStrings(argumentPointers);
        ZeroAndFreeStrings(environmentPointers);
        return NO;
    }
    execve(pythonPath.fileSystemRepresentation, argumentPointers, environmentPointers);
    ZeroAndFreeStrings(argumentPointers);
    ZeroAndFreeStrings(environmentPointers);
    return NO;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *traceID = NSUUID.UUID.UUIDString.lowercaseString;
        traceID = [traceID stringByReplacingOccurrencesOfString:@"-" withString:@""];
        NSString *artifactRoot = nil;

        EmitLog(
            traceID,
            @"launcher_backend",
            LauncherLogInfo,
            @"launcher_process_started",
            @{}
        );

        if (!ConfigureProcessSafety()) {
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"core_limit_unavailable"}
            );
            return 1;
        }
        if (argc != 2) {
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"repository_argument_invalid"}
            );
            return 1;
        }

        NSString *repositoryRoot = [[NSString stringWithUTF8String:argv[1]]
            stringByStandardizingPath].stringByResolvingSymlinksInPath;
        NSString *pythonPath = [repositoryRoot stringByAppendingPathComponent:@".venv/bin/python"];
        NSString *catalogPath = [repositoryRoot
            stringByAppendingPathComponent:@"data/index/catalog-baseline-v1.sqlite3"];
        NSString *gitMarker = [repositoryRoot stringByAppendingPathComponent:@".git"];
        BOOL isDirectory = NO;
        NSFileManager *fileManager = NSFileManager.defaultManager;

        if (
            ![fileManager fileExistsAtPath:gitMarker] ||
            ![fileManager isExecutableFileAtPath:pythonPath] ||
            ![fileManager fileExistsAtPath:catalogPath isDirectory:&isDirectory] ||
            isDirectory
        ) {
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"local_dependency_missing"}
            );
            ShowFailure();
            return 1;
        }
        if (!RequirePortAvailable()) {
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"local_port_in_use", @"port": @(Port)}
            );
            ShowFailure();
            return 1;
        }
        artifactRoot = CreatePrivateArtifactRoot();
        if (artifactRoot == nil) {
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"artifact_root_unavailable"}
            );
            ShowFailure();
            return 1;
        }
        NSString *apiKey = PromptForKey(traceID);
        if (apiKey == nil) {
            RemoveEmptyArtifactRoot(artifactRoot);
            return 0;
        }
        EmitLog(
            traceID,
            @"launcher_backend",
            LauncherLogInfo,
            @"launcher_backend_starting",
            @{
                @"host": Host,
                @"model": ModelID,
                @"port": @(Port),
                @"provider": ProviderID,
            }
        );
        if (!ExecBackend(repositoryRoot, pythonPath, catalogPath, artifactRoot, apiKey)) {
            apiKey = @"";
            RemoveEmptyArtifactRoot(artifactRoot);
            EmitLog(
                traceID,
                @"launcher_backend",
                LauncherLogError,
                @"launcher_backend_failed",
                @{@"error_code": @"backend_exec_failed", @"system_error_code": @(errno)}
            );
            ShowFailure();
            return 1;
        }
    }
    return 1;
}
