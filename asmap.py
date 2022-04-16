import sys
import ipaddress
from collections import namedtuple

Entry = namedtuple('Entry', (
    # The length of the network prefix in bits.
    # For example, 2a01:4f9:c010:19eb::/64 has prefix_len=64.
    # This field must come first, as we'll be sorting entries based on
    # their prefix length.
    'prefix_len',

    # An int containing the bits of the network address. Every
    # address represents a sequence of 128 bits. IPv4 addresses
    # are mapped into the IPv6 range ::ffff:0:0/96.
    'prefix',

    # An int for the autonomous system (AS) number.
    'asn',
))

def txtdata_to_entries(txtdata):
    """
    Given a string corresponding to the contents of a file of the format

        1.0.0.0/24 AS13335 # ipv4.dump:4856343
        1.0.4.0/22 AS56203 # ipv4.dump:2759291
        ...

    Ignoring comments following '#'. Creates an Entry object for each line.
    Maps IPv4 networks into IPv6 space.

    Returns:
        A list containing the Entry objects.
    """
    ret = []
    for line in txtdata.splitlines():
        line = line.split('#')[0].lstrip(' ').rstrip(' \r\n')
        prefix, asn = line.split(' ')
        assert(len(asn) > 2 and asn[:2] == "AS")
        network = ipaddress.ip_network(prefix)

        prefix_len = network.prefixlen
        prefix = int.from_bytes(network.network_address.packed, 'big')

        # Map an IPv4 prefix into IPv6 space.
        if isinstance(network, ipaddress.IPv4Network):
            prefix_len += 96
            prefix += 0xffff00000000

        ret.append(Entry(prefix_len, prefix, int(asn[2:])))

    return ret

def entries_to_txtdata(entries):
    """Convert list of entries to text format."""
    ret = []
    for prefix_len, prefix, asn in entries:
        if prefix_len >= 96 and (prefix >> 32) == 0xffff:
            net = ipaddress.IPv4Network((prefix & 0xffffffff, prefix_len - 96), True)
        else:
            net = ipaddress.IPv6Network((prefix, prefix_len), True)
        ret.append("%s AS%i\n" % (net, asn))
    return "".join(ret)

# Trie representation for asmap
#
# The tree is represented as a recursive structure consisting of list objects.
# Every node is either:
# - []: to indicate "undefined" (no ASN)
# - [int]: to indicate "this entire range maps has ASN int"
# - [left,right]: with left and right new nodes

def entries_to_trie(entries, addrlen=128):
    """
    Construct a trie format representation of the entries in entries.
    In case entries overlap, the smaller range (larger prefix_len) takes
    priority.

    Args:
        entries: The network prefix -> ASN mappings to encode.
        addrlen: The maximum number of bits in a network address.
                 This is 128 for IPv6 (16 bytes).
    Returns:
        The trie.
    """
    trie = []
    for prefix_len, prefix, asn in sorted(entries):
        assert prefix_len <= addrlen
        assert (prefix & ((1 << (addrlen - prefix_len)) - 1)) == 0
        node = trie
        # Iterate through each bit in the network prefix, starting with the
        # most significant bit.
        for i in range(prefix_len):
            bit = (prefix >> (addrlen - 1 - i)) & 1
            if len(node) == 0:
                node += [[], []]
            elif len(node) == 1:
                node[0] = [node[0]]
                node.append(node[0])
            node = node[bit]
        node.clear()
        node.append(asn)

    def simplify(node):
        if len(node) < 2:
            return
        simplify(node[0])
        simplify(node[1])
        if len(node[0]) != len(node[1]):
            return
        if len(node[0]) == 2:
            return
        if len(node[0]) == 0:
            node.clear()
        elif len(node[1]) == 1:
            asn = node[1][0]
            node.clear()
            node.append(asn)

    simplify(trie)

    return trie

def trie_to_entries_flat(trie, addrlen, optimize):
    """Convert a trie to a list of Entry objects that do not overlap."""
    def recurse(node, prefix_len, prefix):
        ret = []
        if len(node) == 1:
            ret = [Entry(prefix_len, prefix << (addrlen - prefix_len), node[0])]
        elif len(node) == 2:
            ret = recurse(node[0], prefix_len + 1, prefix << 1)
            ret += recurse(node[1], prefix_len + 1, (prefix << 1) | 1)
            if optimize and len(ret) > 1:
                asns = set(x.asn for x in ret)
                if len(asns) == 1:
                    ret = [Entry(prefix_len, prefix << (addrlen - prefix_len), list(asns)[0])]
        return ret
    return recurse(trie, 0, 0)

def trie_to_entries_minimal(trie, addrlen, optimize):
    """Convert a trie to a minimal list of Entry objects, exploiting the overlap rule."""
    def recurse(node, prefix_len, prefix):
        if len(node) == 0:
            return ({None: []}, True)
        elif len(node) == 1:
            return ({node[0]: [], None: [Entry(prefix_len, prefix << (addrlen - prefix_len), node[0])]}, False)
        else:
            ret = {}
            left, lhole = recurse(node[0], prefix_len + 1, prefix << 1)
            right, rhole = recurse(node[1], prefix_len + 1, (prefix << 1) | 1)
            hole = lhole or rhole
            for ctx in set(left) & set(right):
                ret[ctx] = left[ctx] + right[ctx]
            for ctx in left:
                if ctx not in ret or len(left[ctx]) + len(right[None]) < len(ret[ctx]):
                    ret[ctx] = left[ctx] + right[None]
            for ctx in right:
                if ctx not in ret or len(left[None]) + len(right[ctx]) < len(ret[ctx]):
                    ret[ctx] = left[None] + right[ctx]
            if optimize or not hole:
                for ctx in ret:
                    if len(ret[ctx]) + 1 < len(ret[None]):
                        ret[None] = [Entry(prefix_len, prefix << (addrlen - prefix_len), ctx)] + ret[ctx]
                return ({ctx:entries for (ctx, entries) in ret.items() if ctx is None or len(entries) < len(ret[None])}, hole)
            else:
                return ({None:ret[None]}, hole)
    return recurse(trie, 0, 0)[0][None]

class AsmapInstruction:
    RETURN = 0
    JUMP = 1
    MATCH = 2
    DEFAULT = 3
    END = 4

def encode_bits(val, minval, bit_sizes) -> [int]:
    """
    Perform a variable-length encoding of a value to bits, least significant
    bit first.

    For each `bit_sizes` passed, attempt to encode the value with that number
    of bits + 1. Normalize the encoded value by `minval` to potentially save
    bits - the value will be corrected during decoding.

    Returns:
        a list of bits representing the value to encode.
    """
    val -= minval
    ret = []
    for pos in range(len(bit_sizes)):
        bit_size = bit_sizes[pos]

        # If the value will not fit in `bit_size` bits, absorb the largest
        # value for this bitsize and continue to the next smallest size.
        if val >= (1 << bit_size):
            val -= (1 << bit_size)
            ret += [1]
        else:
            # If we aren't encoding the largest possible value per the largest
            # bitsize...
            if (pos + 1 < len(bit_sizes)):
                ret += [0]

            # Use remaining bits to encode the rest of val.
            for b in range(bit_size):
                ret += [(val >> (bit_size - 1 - b)) & 1]
            return ret

    # Couldn't fit val into any of the bit_sizes
    assert(False)

def encode_bits_size(val, minval, bit_sizes):
    """Predict the length of encode_bits(val, minval, bit_sizes)."""
    val -= minval
    ret = 0
    for pos in range(len(bit_sizes)):
        bit_size = bit_sizes[pos]
        if val >= (1 << bit_size):
            val -= (1 << bit_size)
            ret += 1
        else:
            if (pos + 1 < len(bit_sizes)):
                ret += 1
            return ret + bit_size
    assert False

BIT_SIZES_TYPE = [0, 0, 1]
BIT_SIZES_ASN = [15, 16, 17, 18, 19, 20, 21, 22, 23, 24]
BIT_SIZES_MATCH = [1, 2, 3, 4, 5, 6, 7, 8]
BIT_SIZES_JUMP = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]

def encode_type(v):
    return encode_bits(v, 0, BIT_SIZES_TYPE)

def encode_type_size(v):
    return encode_bits_size(v, 0, BIT_SIZES_TYPE)

def encode_asn(v):
    return encode_bits(v, 1, BIT_SIZES_ASN)

def encode_asn_size(v):
    return encode_bits_size(v, 1, BIT_SIZES_ASN)

def encode_match(v):
    return encode_bits(v, 2, BIT_SIZES_MATCH)

def encode_match_size(v):
    return encode_bits_size(v, 2, BIT_SIZES_MATCH)

def encode_jump(v):
    return encode_bits(v, 17, BIT_SIZES_JUMP)

def encode_jump_size(v):
    return encode_bits_size(v, 17, BIT_SIZES_JUMP)

class AsmapEncoding:
    @staticmethod
    def predict_size(ins, arg1=None, arg2=None):
        if ins == AsmapInstruction.RETURN:
            assert isinstance(arg1, int)
            assert arg2 is None
            return encode_type_size(ins) + encode_asn_size(arg1)
        elif ins == AsmapInstruction.JUMP:
            assert isinstance(arg1, AsmapEncoding)
            assert isinstance(arg2, AsmapEncoding)
            return encode_type_size(ins) + encode_jump_size(arg1.size) + arg1.size + arg2.size
        elif ins == AsmapInstruction.DEFAULT:
            assert isinstance(arg1, int)
            assert isinstance(arg2, AsmapEncoding)
            return encode_type_size(ins) + encode_asn_size(arg1) + arg2.size
        elif ins == AsmapInstruction.MATCH:
            assert isinstance(arg1, int)
            assert isinstance(arg2, AsmapEncoding)
            return encode_type_size(ins) + encode_match_size(arg1) + arg2.size
        elif ins == AsmapInstruction.END:
            assert arg1 is None
            assert arg2 is None
            return 0
        else:
            assert False

    def __init__(self, ins, arg1=None, arg2=None):
        self.ins = ins
        self.arg1 = arg1
        self.arg2 = arg2
        self.size = self.predict_size(ins, arg1, arg2)

    @staticmethod
    def make_end():
        return AsmapEncoding(AsmapInstruction.END)

    @staticmethod
    def make_leaf(val):
        assert val is not None
        return AsmapEncoding(AsmapInstruction.RETURN, val)

    @staticmethod
    def make_branch(left, right):
        if left.ins == AsmapInstruction.END and right.ins == AsmapInstruction.END:
            return left
        if left.ins == AsmapInstruction.END:
            if right.ins == AsmapInstruction.MATCH and right.arg1 <= 0xFF:
                return AsmapEncoding(right.ins, (right.arg1 << 1) | 1, right.arg2)
            return AsmapEncoding(AsmapInstruction.MATCH, 3, right)
        if right == AsmapInstruction.END:
            if left.ins == AsmapInstruction.MATCH and left.arg1 <= 0xFF:
                return AsmapEncoding(left.ins, left.arg1 << 1, left.arg2)
            return AsmapEncoding(AsmapInstruction.MATCH, 2, left)
        return AsmapEncoding(AsmapInstruction.JUMP, left, right)

    @staticmethod
    def make_default(val, sub):
        assert val is not None
        if sub.ins == AsmapInstruction.END:
            return AsmapEncoding(AsmapInstruction.RETURN, val)
        if sub.ins == AsmapInstruction.RETURN or sub.ins == AsmapInstruction.DEFAULT:
            return sub
        return AsmapEncoding(AsmapInstruction.DEFAULT, val, sub)

    def encode(self):
        if self.ins == AsmapInstruction.RETURN:
            return encode_type(self.ins) + encode_asn(self.arg1)
        elif self.ins == AsmapInstruction.JUMP:
            return encode_type(self.ins) + encode_jump(self.arg1.size) + self.arg1.encode() + self.arg2.encode()
        elif self.ins == AsmapInstruction.DEFAULT:
            return encode_type(self.ins) + encode_asn(self.arg1) + self.arg2.encode()
        elif self.ins == AsmapInstruction.MATCH:
            return encode_type(self.ins) + encode_match(self.arg1) + self.arg2.encode()
        elif self.ins == AsmapInstruction.END:
            return []

def trie_to_encoding(trie, optimize):
    """Convert a trie to asmap encoding."""
    def recurse(node):
        if len(node) == 0:
            return ({(None if optimize else -1): AsmapEncoding.make_end()}, True)
        elif len(node) == 1:
            return ({None: AsmapEncoding.make_leaf(node[0]), node[0]: AsmapEncoding.make_end()}, False)
        else:
            ret = {}
            left, lhole = recurse(node[0])
            right, rhole = recurse(node[1])
            hole = lhole or rhole
            for ctx in set(left) & set(right):
                ret[ctx] = AsmapEncoding.make_branch(left[ctx], right[ctx])
            if None in left:
                for ctx in right:
                    cand = AsmapEncoding.make_branch(left[None], right[ctx])
                    if ctx not in ret or cand.size < ret[ctx].size:
                        ret[ctx] = cand
            if None in right:
                for ctx in left:
                    cand = AsmapEncoding.make_branch(left[ctx], right[None])
                    if ctx not in ret or cand.size < ret[ctx].size:
                        ret[ctx] = cand
            if optimize or not hole:
                gen = ret.get(None, None)
                for ctx in ret:
                    if ctx is not None and ctx != -1:
                        cand = AsmapEncoding.make_default(ctx, ret[ctx])
                        if gen is None or cand.size < gen.size:
                            gen = cand
                if gen is not None:
                    ret[None] = gen
                return ({ctx:enc for (ctx,enc) in ret.items() if ctx is None or gen is None or enc.size < gen.size}, hole)
            else:
                return ({ctx:enc for (ctx,enc) in ret.items() if ctx is None or ctx == -1}, hole)
    res, _ = recurse(trie)
    if -1 in res:
        return res[-1]
    if None in res:
        return res[None]



print("Reading file...")
txtdata = sys.stdin.read()
print("Parsing file...")
entries = txtdata_to_entries(txtdata)
print("Building trie...")
trie = entries_to_trie(entries)
print("Building flat entries list...")
e_flat_unopt = trie_to_entries_flat(trie, 128, False)
print(len(e_flat_unopt))
print("Building optimized flat entries list...")
e_flat_opt = trie_to_entries_flat(trie, 128, True)
print(len(e_flat_opt))
print("Building minimal entries list...")
e_min_unopt = trie_to_entries_minimal(trie, 128, False)
print(len(e_min_unopt))
print("Building optimized minimal entries list...")
e_min_opt = trie_to_entries_minimal(trie, 128, True)
print(len(e_min_opt))
print("Building encoding...")
enc_unopt = trie_to_encoding(trie, False)
print(enc_unopt.size)
print("Building optimized encoding...")
enc_opt = trie_to_encoding(trie, True)
print(enc_opt.size)

with open("asmap_flat_unopt.txt", "w") as f:
    f.write(entries_to_txtdata(e_flat_unopt))
with open("asmap_flat_opt.txt", "w") as f:
    f.write(entries_to_txtdata(e_flat_opt))
with open("asmap_min_unopt.txt", "w") as f:
    f.write(entries_to_txtdata(e_min_unopt))
with open("asmap_min_opt.txt", "w") as f:
    f.write(entries_to_txtdata(e_min_opt))
