#pragma once

#include <cstring>
#include <vector>
#include <lwip/sockets.h>
#include <arpa/inet.h>
#include <fcntl.h>

// Minimal single-client raw-PCM TCP server. The Atom listens; client
// connects and pulls audio. A new incoming connection replaces whatever
// client was previously attached.
namespace audio_server {

static int g_listen_fd = -1;
static int g_client_fd = -1;

inline void init(uint16_t port) {
  if (g_listen_fd >= 0)
    return;

  g_listen_fd = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

  int reuse = 1;
  setsockopt(g_listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

  struct sockaddr_in addr = {};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = INADDR_ANY;
  addr.sin_port = htons(port);
  bind(g_listen_fd, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr));
  listen(g_listen_fd, 1);

  int flags = fcntl(g_listen_fd, F_GETFL, 0);
  fcntl(g_listen_fd, F_SETFL, flags | O_NONBLOCK);
}

// Non-blocking; call often (piggybacked on microphone on_data below) so a
// new connection is picked up promptly without a dedicated polling task.
inline void poll_accept() {
  if (g_listen_fd < 0)
    return;
  int fd = accept(g_listen_fd, nullptr, nullptr);
  if (fd >= 0) {
    if (g_client_fd >= 0)
      close(g_client_fd);
    int flags = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    g_client_fd = fd;
  }
}

inline void send(const std::vector<uint8_t> &data) {
  if (g_client_fd < 0)
    return;
  ssize_t sent = ::send(g_client_fd, data.data(), data.size(), MSG_DONTWAIT);
  if (sent < 0) {
    close(g_client_fd);
    g_client_fd = -1;
  }
}

inline bool has_client() { return g_client_fd >= 0; }

}  // namespace audio_server
