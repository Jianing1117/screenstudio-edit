#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

// Apple Vision runs locally. Output boxes use top-left normalized coordinates.
int main(int argc, const char *argv[]) { @autoreleasepool {
    NSMutableArray *output = [NSMutableArray array];
    for (int i = 1; i < argc; i++) {
        NSString *path = [NSString stringWithUTF8String:argv[i]];
        VNRecognizeTextRequest *request = [VNRecognizeTextRequest new];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
        request.usesLanguageCorrection = NO;
        NSError *error = nil;
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
            initWithURL:[NSURL fileURLWithPath:path] options:@{}];
        if (![handler performRequests:@[request] error:&error]) {
            fprintf(stderr, "Local OCR failed\n"); return 2;
        }
        NSMutableArray *lines = [NSMutableArray array];
        for (VNRecognizedTextObservation *item in request.results) {
            VNRecognizedText *text = [[item topCandidates:1] firstObject];
            if (!text) continue;
            CGRect b = item.boundingBox;
            [lines addObject:@{@"text": text.string, @"confidence": @(text.confidence),
                @"box01": @[@(b.origin.x), @(1-b.origin.y-b.size.height), @(b.size.width), @(b.size.height)]}];
        }
        [output addObject:@{@"file": path, @"lines": lines}];
    }
    NSData *data = [NSJSONSerialization dataWithJSONObject:output options:0 error:nil];
    fwrite(data.bytes, 1, data.length, stdout);
} return 0; }
