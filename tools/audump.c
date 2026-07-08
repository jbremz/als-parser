/* audump — instantiate an installed Audio Unit and dump its state layout.
 *
 * Usage:  audump TYPE SUBTYPE MANU [classinfo.plist]
 *   e.g.  audump aufx Gaff Klev /tmp/gaffel.plist
 *
 * Prints the AU's parameter table (id, min, max, default, current, name) to
 * stdout and optionally writes the default kAudioUnitProperty_ClassInfo dict
 * (i.e. a .aupreset) as an XML plist. Used by the VST2 preset-recovery tooling
 * to learn a plugin's aupreset layout and JUCE-hashed parameter ids without
 * needing the user to instantiate anything in a DAW.
 *
 * Build:  clang -framework AudioToolbox -framework CoreFoundation \
 *               -o audump audump.c
 */
#include <AudioToolbox/AudioToolbox.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static UInt32 fourcc(const char *s) {
    return ((UInt32)(unsigned char)s[0] << 24) | ((UInt32)(unsigned char)s[1] << 16)
         | ((UInt32)(unsigned char)s[2] << 8)  |  (UInt32)(unsigned char)s[3];
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: audump TYPE SUBTYPE MANU [classinfo.plist]\n");
        return 2;
    }
    AudioComponentDescription desc = {0};
    desc.componentType = fourcc(argv[1]);
    desc.componentSubType = fourcc(argv[2]);
    desc.componentManufacturer = fourcc(argv[3]);

    AudioComponent comp = AudioComponentFindNext(NULL, &desc);
    if (!comp) { fprintf(stderr, "component not found\n"); return 1; }

    AudioUnit au;
    OSStatus err = AudioComponentInstanceNew(comp, &au);
    if (err) { fprintf(stderr, "instantiate failed: %d\n", (int)err); return 1; }
    err = AudioUnitInitialize(au);
    if (err) fprintf(stderr, "warning: init failed (%d), continuing\n", (int)err);

    if (argc > 4) {
        CFPropertyListRef pl = NULL;
        UInt32 sz = sizeof(pl);
        err = AudioUnitGetProperty(au, kAudioUnitProperty_ClassInfo,
                                   kAudioUnitScope_Global, 0, &pl, &sz);
        if (!err && pl) {
            CFDataRef xml = CFPropertyListCreateData(NULL, pl,
                kCFPropertyListXMLFormat_v1_0, 0, NULL);
            FILE *f = fopen(argv[4], "wb");
            fwrite(CFDataGetBytePtr(xml), 1, (size_t)CFDataGetLength(xml), f);
            fclose(f);
            CFRelease(xml);
        } else {
            fprintf(stderr, "ClassInfo get failed: %d\n", (int)err);
        }
    }

    UInt32 psz = 0;
    AudioUnitGetPropertyInfo(au, kAudioUnitProperty_ParameterList,
                             kAudioUnitScope_Global, 0, &psz, NULL);
    UInt32 n = psz / (UInt32)sizeof(AudioUnitParameterID);
    AudioUnitParameterID *ids = malloc(psz);
    AudioUnitGetProperty(au, kAudioUnitProperty_ParameterList,
                         kAudioUnitScope_Global, 0, ids, &psz);
    printf("PARAMS %u\n", (unsigned)n);
    for (UInt32 i = 0; i < n; i++) {
        AudioUnitParameterInfo info;
        UInt32 isz = sizeof(info);
        if (AudioUnitGetProperty(au, kAudioUnitProperty_ParameterInfo,
                                 kAudioUnitScope_Global, ids[i], &info, &isz))
            continue;
        char name[256] = {0};
        if ((info.flags & kAudioUnitParameterFlag_HasCFNameString) && info.cfNameString)
            CFStringGetCString(info.cfNameString, name, sizeof(name),
                               kCFStringEncodingUTF8);
        else
            strncpy(name, info.name, sizeof(name) - 1);
        AudioUnitParameterValue val = 0;
        AudioUnitGetParameter(au, ids[i], kAudioUnitScope_Global, 0, &val);
        printf("P\t%u\t%g\t%g\t%g\t%g\t%s\n", (unsigned)ids[i],
               info.minValue, info.maxValue, info.defaultValue, val, name);
    }
    free(ids);
    AudioUnitUninitialize(au);
    AudioComponentInstanceDispose(au);
    return 0;
}
