#include <AudioToolbox/AudioToolbox.h>
#include <stdio.h>
static void pc(UInt32 v){ for(int i=3;i>=0;i--){ char c=(v>>(8*i))&0xff; putchar(c>=32&&c<127?c:'?'); } }
int main(void){
    AudioComponentDescription want={0};
    AudioComponent c=NULL;
    while((c=AudioComponentFindNext(c,&want))){
        AudioComponentDescription d; AudioComponentGetDescription(c,&d);
        CFStringRef nm=NULL; AudioComponentCopyName(c,&nm);
        char buf[256]={0};
        if(nm){ CFStringGetCString(nm,buf,sizeof(buf),kCFStringEncodingUTF8); CFRelease(nm); }
        pc(d.componentType); putchar('\t'); pc(d.componentSubType); putchar('\t'); pc(d.componentManufacturer);
        printf("\t%u\t%u\t%u\t%s\n",(unsigned)d.componentType,(unsigned)d.componentSubType,(unsigned)d.componentManufacturer,buf);
    }
    return 0;
}
