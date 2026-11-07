#ifndef FW_USBCON_H
#define FW_USBCON_H

/* USB serial console experiment (CDC-ACM on the printer's USB port, see
   usbcon.c). usbcon_poll() runs from the idle loop; usbcon_getc() returns
   the next character typed on the USB side, -1 if none. */
void usbcon_poll(void);
int  usbcon_getc(void);
void usbcon_start(void);
void usbcon_stop(void);                   /* pull-up off, endpoints off, no more DMA */                  /* "usb on" (printer builds: at boot, see fw/build.py) */
int  cmd_usb(int argc, char **argv);

#define USB_CMD_ENTRY \
    { "usb", "usb [status] | on | off | uart on|off: serial console on the USB port (CDC-ACM)", cmd_usb }

#endif
