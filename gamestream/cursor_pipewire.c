/* Separate, read-only PipeWire consumer for cursor metadata. Uses public APIs;
 * does not preload, interpose, or alter GStreamer's buffers or callbacks. */
#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/buffers.h>
#include <spa/pod/builder.h>
#include <spa/buffer/meta.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>

struct reader { struct pw_main_loop *loop; struct pw_stream *stream;
    struct spa_hook listener; uint32_t width, height; };

static void quit(void *data, int signal_number) {
    (void)signal_number;
    pw_main_loop_quit(((struct reader *)data)->loop);
}
static void state_changed(void *data, enum pw_stream_state old,
        enum pw_stream_state state, const char *error) {
    (void)old;
    if (state == PW_STREAM_STATE_ERROR) {
        fprintf(stderr, "Cursor metadata: %s\n", error ? error : "stream error");
        pw_main_loop_quit(((struct reader *)data)->loop);
    }
}
static void format_changed(void *data, uint32_t id, const struct spa_pod *param) {
    struct reader *r = data;
    if (id != SPA_PARAM_Format || !param) return;
    struct spa_video_info_raw info = {0};
    if (spa_format_video_raw_parse(param, &info) < 0) return;
    r->width = info.size.width; r->height = info.size.height;
    uint8_t storage[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *params[2];
    params[0] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
        SPA_PARAM_BUFFERS_buffers, SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
        SPA_PARAM_BUFFERS_dataType, SPA_POD_CHOICE_FLAGS_Int((1<<SPA_DATA_MemFd) | (1<<SPA_DATA_MemPtr)));
    int max_size = sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + 384*384*4;
    params[1] = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(max_size, sizeof(struct spa_meta_cursor), max_size));
    pw_stream_update_params(r->stream, params, 2);
}
static void process(void *data) {
    struct reader *r = data;
    struct pw_buffer *buffer;
    while ((buffer = pw_stream_dequeue_buffer(r->stream))) {
        struct spa_meta *meta = spa_buffer_find_meta(buffer->buffer, SPA_META_Cursor);
        if (meta && meta->data && meta->size >= sizeof(struct spa_meta_cursor) && r->width && r->height) {
            const struct spa_meta_cursor *cursor = meta->data;
            if (cursor->id) {
                printf("{\"x\":%d,\"y\":%d,\"width\":%u,\"height\":%u", cursor->position.x, cursor->position.y, r->width, r->height);
                uint32_t off = cursor->bitmap_offset;
                if (off >= sizeof(*cursor) && off <= meta->size - sizeof(struct spa_meta_bitmap)) {
                    const struct spa_meta_bitmap *bitmap = SPA_PTROFF(cursor, off, const struct spa_meta_bitmap);
                    uint32_t w = bitmap->size.width, h = bitmap->size.height;
                    if (!bitmap->offset || !bitmap->format || !w || !h) {
                        printf(",\"visible\":false");
                    } else if (w <= 384 && h <= 384 && bitmap->stride >= (int32_t)w*4 &&
                            bitmap->offset >= sizeof(*bitmap) &&
                            (uint64_t)off + bitmap->offset + (uint64_t)bitmap->stride*(h-1) + w*4 <= meta->size &&
                            (bitmap->format == SPA_VIDEO_FORMAT_RGBA || bitmap->format == SPA_VIDEO_FORMAT_BGRA)) {
                        const uint8_t *pixels = SPA_PTROFF(bitmap, bitmap->offset, const uint8_t);
                        int visible = 0;
                        for (uint32_t y=0; y<h; y++) for (uint32_t x=0; x<w; x++)
                            visible |= pixels[y*bitmap->stride+x*4+3];
                        printf(",\"visible\":%s,\"image_width\":%u,\"image_height\":%u,\"hotspot\":[%d,%d],\"rgba_hex\":\"", visible ? "true" : "false", w, h, cursor->hotspot.x, cursor->hotspot.y);
                        for (uint32_t y=0; y<h; y++) for (uint32_t x=0; x<w; x++) {
                            const uint8_t *p = pixels + y*bitmap->stride + x*4;
                            if (bitmap->format == SPA_VIDEO_FORMAT_BGRA) printf("%02x%02x%02x%02x",p[2],p[1],p[0],p[3]);
                            else printf("%02x%02x%02x%02x",p[0],p[1],p[2],p[3]);
                        }
                        printf("\"");
                    }
                }
                puts("}"); fflush(stdout);
            }
        }
        pw_stream_queue_buffer(r->stream, buffer);
    }
}
static const struct pw_stream_events events = {
    PW_VERSION_STREAM_EVENTS, .state_changed=state_changed, .param_changed=format_changed, .process=process
};
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    pw_init(&argc, &argv);
    struct reader r = {0};
    r.loop = pw_main_loop_new(NULL);
    if (!r.loop) return 1;
    struct pw_context *context = pw_context_new(pw_main_loop_get_loop(r.loop), NULL, 0);
    int fd = atoi(argv[2]);
    struct pw_core *core = fd >= 0 ? pw_context_connect_fd(context, fd, NULL, 0) : pw_context_connect(context, NULL, 0);
    if (!core) { pw_context_destroy(context); pw_main_loop_destroy(r.loop); return 1; }
    r.stream = pw_stream_new(core, "Scry cursor metadata", pw_properties_new(PW_KEY_MEDIA_TYPE,"Video",PW_KEY_MEDIA_CATEGORY,"Capture",NULL));
    pw_stream_add_listener(r.stream, &r.listener, &events, &r);
    uint8_t storage[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(storage, sizeof(storage));
    const struct spa_pod *format = spa_pod_builder_add_object(&b, SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(4,SPA_VIDEO_FORMAT_RGBA,SPA_VIDEO_FORMAT_BGRA,SPA_VIDEO_FORMAT_RGBx,SPA_VIDEO_FORMAT_BGRx),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&SPA_RECTANGLE(1920,1080),&SPA_RECTANGLE(1,1),&SPA_RECTANGLE(16384,16384)),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_CHOICE_RANGE_Fraction(&SPA_FRACTION(0,1),&SPA_FRACTION(0,1),&SPA_FRACTION(240,1)));
    int result = pw_stream_connect(r.stream,PW_DIRECTION_INPUT,(uint32_t)strtoul(argv[1],NULL,10),PW_STREAM_FLAG_AUTOCONNECT|PW_STREAM_FLAG_MAP_BUFFERS,&format,1);
    pw_loop_add_signal(pw_main_loop_get_loop(r.loop), SIGTERM, quit, &r);
    pw_loop_add_signal(pw_main_loop_get_loop(r.loop), SIGINT, quit, &r);
    if (result >= 0) pw_main_loop_run(r.loop);
    pw_stream_destroy(r.stream); pw_core_disconnect(core); pw_context_destroy(context); pw_main_loop_destroy(r.loop); pw_deinit();
    return result < 0 ? 1 : 0;
}
