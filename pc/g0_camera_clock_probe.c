#define _POSIX_C_SOURCE 200809L
#include <time.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <stdint.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

/* Finite timestamp-domain probe. No image copying, format/control writes,
 * RF or board access. Run only under the shared hardware operation lease. */
int main(void)
{
 int fd=open("/dev/video0",O_RDWR|O_NONBLOCK),rc=1,streaming=0,printed=0;
 void *maps[4]={0};size_t sizes[4]={0};unsigned mapped=0,received=0;
 enum v4l2_buf_type type=V4L2_BUF_TYPE_VIDEO_CAPTURE;
 struct v4l2_format fmt={.type=V4L2_BUF_TYPE_VIDEO_CAPTURE};
 struct v4l2_requestbuffers req={.count=4,.type=V4L2_BUF_TYPE_VIDEO_CAPTURE,.memory=V4L2_MEMORY_MMAP};
 if(fd<0){perror("open camera");return 1;}
 if(ioctl(fd,VIDIOC_G_FMT,&fmt)){perror("G_FMT");goto done;}
 printf("{\"width\":%u,\"height\":%u,\"fourcc\":%u,\"buffers\":[",fmt.fmt.pix.width,fmt.fmt.pix.height,fmt.fmt.pix.pixelformat);printed=1;
 if(ioctl(fd,VIDIOC_REQBUFS,&req)||req.count<2||req.count>4){perror("REQBUFS");goto done;}
 for(unsigned i=0;i<req.count;i++){
  struct v4l2_buffer b={.type=type,.memory=V4L2_MEMORY_MMAP,.index=i};
  if(ioctl(fd,VIDIOC_QUERYBUF,&b)||!b.length||b.length>4194304){perror("QUERYBUF");goto done;}
  maps[i]=mmap(NULL,b.length,PROT_READ,MAP_SHARED,fd,b.m.offset);
  if(maps[i]==MAP_FAILED){maps[i]=NULL;perror("mmap");goto done;}
  sizes[i]=b.length;mapped++;
  if(ioctl(fd,VIDIOC_QBUF,&b)){perror("QBUF");goto done;}
 }
 if(ioctl(fd,VIDIOC_STREAMON,&type)){perror("STREAMON");goto done;}streaming=1;
 for(unsigned i=0;i<3;i++){
  struct pollfd p={.fd=fd,.events=POLLIN};
  struct v4l2_buffer b={.type=type,.memory=V4L2_MEMORY_MMAP};struct timespec now;
  if(poll(&p,1,2000)<=0||ioctl(fd,VIDIOC_DQBUF,&b)){perror("DQBUF");goto done;}
  if(clock_gettime(CLOCK_MONOTONIC,&now)){perror("CLOCK_MONOTONIC");goto done;}
  uint64_t capture=(uint64_t)b.timestamp.tv_sec*1000000000+(uint64_t)b.timestamp.tv_usec*1000;
  uint64_t observed=(uint64_t)now.tv_sec*1000000000+(uint64_t)now.tv_nsec;
  printf("%s{\"flags\":%u,\"timestamp_type\":%u,\"timestamp_source\":%u,\"capture_ns\":%"PRIu64",\"observed_monotonic_ns\":%"PRIu64"}",
   received?",":"",b.flags,b.flags&V4L2_BUF_FLAG_TIMESTAMP_MASK,b.flags&V4L2_BUF_FLAG_TSTAMP_SRC_MASK,capture,observed);
  received++;
  if((b.flags&V4L2_BUF_FLAG_TIMESTAMP_MASK)!=V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC||capture>observed||observed-capture>2000000000ULL){rc=2;goto done;}
  if(ioctl(fd,VIDIOC_QBUF,&b)){perror("requeue");goto done;}
 }
 rc=0;
done:
 if(streaming&&ioctl(fd,VIDIOC_STREAMOFF,&type))rc=3;
 for(unsigned i=0;i<mapped;i++)if(maps[i]&&munmap(maps[i],sizes[i]))rc=3;
 req.count=0;if(ioctl(fd,VIDIOC_REQBUFS,&req))rc=3;
 if(printed)printf("],\"received\":%u,\"format_or_controls_changed\":false,\"completed\":%s}\n",received,rc==0?"true":"false");
 close(fd);return rc;
}
