import os
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, IntEnum
from struct import pack_into, unpack_from
from typing import List, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

TLS_VERSION_1_2 = 0x0303
TLS_VERSION_1_3 = 0x0304
TLS_VERSION_1_3_DRAFT_28 = 0x7f1c
TLS_VERSION_1_3_DRAFT_27 = 0x7f1b
TLS_VERSION_1_3_DRAFT_26 = 0x7f1a

TLS_HANDSHAKE_CLIENT_HELLO = 1
TLS_HANDSHAKE_SERVER_HELLO = 2


class Direction(Enum):
    DECRYPT = 0
    ENCRYPT = 1


class Epoch(Enum):
    HANDSHAKE = 2


def hkdf_label(label, hash_value, length):
    full_label = b'tls13 ' + label
    return (
        struct.pack('!HB', length, len(full_label)) + full_label +
        struct.pack('!B', len(hash_value)) + hash_value)


def hkdf_expand_label(algorithm, secret, label, hash_value, length):
    return HKDFExpand(
        algorithm=algorithm,
        length=length,
        info=hkdf_label(label, hash_value, length),
        backend=default_backend()
    ).derive(secret)


def hkdf_extract(algorithm, salt, key_material):
    h = hmac.HMAC(salt, algorithm, backend=default_backend())
    h.update(key_material)
    return h.finalize()


class CipherSuite(IntEnum):
    AES_256_GCM_SHA384 = 0x1302
    AES_128_GCM_SHA256 = 0x1301
    CHACHA20_POLY1305_SHA256 = 0x1303


class CompressionMethod(IntEnum):
    NULL = 0


class ExtensionType(IntEnum):
    SUPPORTED_GROUPS = 10
    SIGNATURE_ALGORITHMS = 13
    SUPPORTED_VERSIONS = 43
    PSK_KEY_EXCHANGE_MODES = 45
    KEY_SHARE = 51
    QUIC_TRANSPORT_PARAMETERS = 65445


class Group(IntEnum):
    SECP256R1 = 23


class KeyExchangeMode(IntEnum):
    PSK_DHE_KE = 1


class SignatureAlgorithm(IntEnum):
    RSA_PSS_RSAE_SHA256 = 0x0804
    ECDSA_SECP256R1_SHA256 = 0x0403
    RSA_PKCS1_SHA256 = 0x0401
    RSA_PKCS1_SHA1 = 0x0201


class BufferReadError(ValueError):
    pass


class Buffer:
    def __init__(self, capacity=None, data=None):
        if data is not None:
            self._data = data
            self._length = len(data)
        else:
            self._data = bytearray(capacity)
            self._length = capacity
        self._pos = 0

    @property
    def data(self):
        return bytes(self._data[:self._pos])

    def data_slice(self, start, end):
        return bytes(self._data[start:end])

    def eof(self):
        return self._pos == self._length

    def seek(self, pos):
        assert pos <= self._length
        self._pos = pos

    def tell(self):
        return self._pos


@dataclass
class ClientHello:
    random: bytes = None
    session_id: bytes = None
    cipher_suites: List[int] = None
    compression_methods: List[int] = None

    # extensions
    key_exchange_modes: List[int] = None
    key_share: List[Tuple[int, bytes]] = None
    signature_algorithms: List[int] = None
    supported_groups: List[int] = None
    supported_versions: List[int] = None


@dataclass
class ServerHello:
    random: bytes = None
    session_id: bytes = None
    cipher_suite: int = None
    compression_method: int = None

    # extensions
    key_share: Tuple[int, bytes] = None
    supported_version: int = None


# BYTES


def pull_bytes(buf, length):
    """
    Pull bytes.
    """
    if buf._pos + length > buf._length:
        raise BufferReadError
    v = buf._data[buf._pos:buf._pos + length]
    buf._pos += length
    return v


def push_bytes(buf, v):
    """
    Push bytes.
    """
    length = len(v)
    buf._data[buf._pos:buf._pos + length] = v
    buf._pos += length


# INTEGERS


def pull_uint8(buf):
    """
    Pull an 8-bit unsigned integer.
    """
    try:
        v = buf._data[buf._pos]
        buf._pos += 1
        return v
    except IndexError:
        raise BufferReadError


def push_uint8(buf, v):
    """
    Push an 8-bit unsigned integer.
    """
    buf._data[buf._pos] = v
    buf._pos += 1


def pull_uint16(buf):
    """
    Pull a 16-bit unsigned integer.
    """
    try:
        v, = struct.unpack_from('!H', buf._data, buf._pos)
        buf._pos += 2
        return v
    except struct.error:
        raise BufferReadError


def push_uint16(buf, v):
    """
    Push a 16-bit unsigned integer.
    """
    pack_into('!H', buf._data, buf._pos, v)
    buf._pos += 2


def pull_uint32(buf):
    """
    Pull a 32-bit unsigned integer.
    """
    try:
        v, = struct.unpack_from('!L', buf._data, buf._pos)
        buf._pos += 4
        return v
    except struct.error:
        raise BufferReadError


def push_uint32(buf, v):
    """
    Push a 32-bit unsigned integer.
    """
    pack_into('!L', buf._data, buf._pos, v)
    buf._pos += 4


def pull_uint64(buf):
    """
    Pull a 64-bit unsigned integer.
    """
    try:
        v, = unpack_from('!Q', buf._data, buf._pos)
        buf._pos += 8
        return v
    except struct.error:
        raise BufferReadError


def push_uint64(buf, v):
    """
    Push a 64-bit unsigned integer.
    """
    pack_into('!Q', buf._data, buf._pos, v)
    buf._pos += 8


# BLOCKS


@contextmanager
def pull_block(buf, capacity):
    length = 0
    for b in pull_bytes(buf, capacity):
        length = (length << 8) | b
    end = buf._pos + length
    yield end
    assert buf._pos == end


@contextmanager
def push_block(buf, capacity):
    """
    Context manager to push a variable-length block, with `capacity` bytes
    to write the length.
    """
    buf._pos += capacity
    start = buf._pos
    yield
    length = buf._pos - start
    while capacity:
        buf._data[start - capacity] = (length >> (8 * (capacity - 1))) & 0xff
        capacity -= 1


# LISTS


def pull_list(buf, capacity, func):
    """
    Pull a list of items.
    """
    items = []
    with pull_block(buf, capacity) as end:
        while buf._pos < end:
            items.append(func(buf))
    return items


def push_list(buf, capacity, func, values):
    """
    Push a list of items.
    """
    with push_block(buf, capacity):
        for value in values:
            func(buf, value)


# KeyShareEntry


def pull_key_share(buf):
    group = pull_uint16(buf)
    data_length = pull_uint16(buf)
    data = pull_bytes(buf, data_length)
    return (group, data)


def push_key_share(buf, value):
    push_uint16(buf, value[0])
    with push_block(buf, 2):
        push_bytes(buf, value[1])


# QuicTransportParameters


def push_tlv8(buf, param, value):
    push_uint16(buf, param)
    push_uint16(buf, 1)
    push_uint8(buf, value)


def push_tlv16(buf, param, value):
    push_uint16(buf, param)
    push_uint16(buf, 2)
    push_uint16(buf, value)


def push_tlv32(buf, param, value):
    push_uint16(buf, param)
    push_uint16(buf, 4)
    push_uint32(buf, value)


def push_quic_transport_parameters(buf, value):
    push_uint32(buf, 0xff000011)  # QUIC draft 17
    with push_block(buf, 2):
        push_tlv32(buf, 0x0005, 0x80100000)
        push_tlv32(buf, 0x0006, 0x80100000)
        push_tlv32(buf, 0x0007, 0x80100000)
        push_tlv32(buf, 0x0004, 0x81000000)
        push_tlv16(buf, 0x0001, 0x4258)
        push_tlv16(buf, 0x0008, 0x4064)
        push_tlv8(buf, 0x000a, 0x0a)


@contextmanager
def push_extension(buf, extension_type):
    push_uint16(buf, extension_type)
    with push_block(buf, 2):
        yield


# CLIENT HELLO

def pull_client_hello(buf):
    hello = ClientHello()

    assert pull_uint8(buf) == TLS_HANDSHAKE_CLIENT_HELLO
    with pull_block(buf, 3):
        assert pull_uint16(buf) == TLS_VERSION_1_2
        hello.random = pull_bytes(buf, 32)

        session_id_length = pull_uint8(buf)
        hello.session_id = pull_bytes(buf, session_id_length)

        hello.cipher_suites = pull_list(buf, 2, pull_uint16)
        hello.compression_methods = pull_list(buf, 1, pull_uint8)

        # extensions
        def pull_extension(buf):
            extension_type = pull_uint16(buf)
            extension_length = pull_uint16(buf)
            if extension_type == ExtensionType.KEY_SHARE:
                hello.key_share = pull_list(buf, 2, pull_key_share)
            elif extension_type == ExtensionType.SUPPORTED_VERSIONS:
                hello.supported_versions = pull_list(buf, 1, pull_uint16)
            elif extension_type == ExtensionType.SIGNATURE_ALGORITHMS:
                hello.signature_algorithms = pull_list(buf, 2, pull_uint16)
            elif extension_type == ExtensionType.SUPPORTED_GROUPS:
                hello.supported_groups = pull_list(buf, 2, pull_uint16)
            elif extension_type == ExtensionType.PSK_KEY_EXCHANGE_MODES:
                hello.key_exchange_modes = pull_list(buf, 1, pull_uint8)
            else:
                pull_bytes(buf, extension_length)

        pull_list(buf, 2, pull_extension)

    return hello


def push_client_hello(buf, hello):
    push_uint8(buf, TLS_HANDSHAKE_CLIENT_HELLO)
    with push_block(buf, 3):
        push_uint16(buf, TLS_VERSION_1_2)
        push_bytes(buf, hello.random)
        with push_block(buf, 1):
            push_bytes(buf, hello.session_id)
        push_list(buf, 2, push_uint16, hello.cipher_suites)
        push_list(buf, 1, push_uint8, hello.compression_methods)

        # extensions
        with push_block(buf, 2):
            with push_extension(buf, ExtensionType.KEY_SHARE):
                push_list(buf, 2, push_key_share, hello.key_share)

            with push_extension(buf, ExtensionType.SUPPORTED_VERSIONS):
                push_list(buf, 1, push_uint16, hello.supported_versions)

            with push_extension(buf, ExtensionType.SIGNATURE_ALGORITHMS):
                push_list(buf, 2, push_uint16, hello.signature_algorithms)

            with push_extension(buf, ExtensionType.SUPPORTED_GROUPS):
                push_list(buf, 2, push_uint16, hello.supported_groups)

            with push_extension(buf, ExtensionType.QUIC_TRANSPORT_PARAMETERS):
                push_quic_transport_parameters(buf, None)

            with push_extension(buf, ExtensionType.PSK_KEY_EXCHANGE_MODES):
                push_list(buf, 1, push_uint8, hello.key_exchange_modes)


# SERVER HELLO


def pull_server_hello(buf):
    hello = ServerHello()

    assert pull_uint8(buf) == TLS_HANDSHAKE_SERVER_HELLO
    with pull_block(buf, 3):
        assert pull_uint16(buf) == TLS_VERSION_1_2
        hello.random = pull_bytes(buf, 32)
        session_id_length = pull_uint8(buf)
        hello.session_id = pull_bytes(buf, session_id_length)
        hello.cipher_suite = pull_uint16(buf)
        hello.compression_method = pull_uint8(buf)

        # extensions
        def pull_extension(buf):
            extension_type = pull_uint16(buf)
            extension_length = pull_uint16(buf)
            if extension_type == ExtensionType.SUPPORTED_VERSIONS:
                hello.supported_version = pull_uint16(buf)
            elif extension_type == ExtensionType.KEY_SHARE:
                hello.key_share = pull_key_share(buf)
            else:
                pull_bytes(buf, extension_length)

        pull_list(buf, 2, pull_extension)

    return hello


def push_server_hello(buf, hello):
    push_uint8(buf, TLS_HANDSHAKE_SERVER_HELLO)
    with push_block(buf, 3):
        push_uint16(buf, TLS_VERSION_1_2)
        push_bytes(buf, hello.random)

        with push_block(buf, 1):
            push_bytes(buf, hello.session_id)

        push_uint16(buf, hello.cipher_suite)
        push_uint8(buf, hello.compression_method)

        # extensions
        with push_block(buf, 2):
            with push_extension(buf, ExtensionType.SUPPORTED_VERSIONS):
                push_uint16(buf, hello.supported_version)

            with push_extension(buf, ExtensionType.KEY_SHARE):
                push_key_share(buf, hello.key_share)


class State(Enum):
    CLIENT_HANDSHAKE_START = 0
    CLIENT_EXPECT_SERVER_HELLO = 1
    CLIENT_EXPECT_ENCRYPTED_EXTENSIONS = 2
    SERVER_EXPECT_CLIENT_HELLO = 3
    SERVER_EXPECT_FINISHED = 4


class KeySchedule:
    def __init__(self):
        self.algorithm = hashes.SHA256()
        self.generation = 0
        self.hash = hashes.Hash(self.algorithm, default_backend())
        self.hash_empty_value = self.hash.copy().finalize()
        self.secret = bytes(self.algorithm.digest_size)

    def derive_secret(self, label):
        return hkdf_expand_label(
            algorithm=self.algorithm,
            secret=self.secret,
            label=label,
            hash_value=self.hash.copy().finalize(),
            length=self.algorithm.digest_size)

    def extract(self, key_material=None):
        if key_material is None:
            key_material = bytes(self.algorithm.digest_size)

        if self.generation:
            self.secret = hkdf_expand_label(
                algorithm=self.algorithm,
                secret=self.secret,
                label=b'derived',
                hash_value=self.hash_empty_value,
                length=self.algorithm.digest_size)

        self.generation += 1
        self.secret = hkdf_extract(
            algorithm=self.algorithm,
            salt=self.secret,
            key_material=key_material)

    def update_hash(self, data):
        self.hash.update(data)


class Context:
    def __init__(self, is_client):
        self.enc_key = None
        self.dec_key = None
        self.update_traffic_key_cb = lambda d, e, s: None

        if is_client:
            self.client_random = os.urandom(32)
            self.session_id = os.urandom(32)
            self.private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
            self.state = State.CLIENT_HANDSHAKE_START
        else:
            self.client_random = None
            self.session_id = None
            self.private_key = None
            self.state = State.SERVER_EXPECT_CLIENT_HELLO

        self.is_client = is_client

    def handle_message(self, input_data, output_buf):
        if self.state == State.CLIENT_HANDSHAKE_START:
            self._client_send_hello(output_buf)
        elif self.state == State.CLIENT_EXPECT_SERVER_HELLO:
            self._client_handle_hello(input_data, output_buf)
        elif self.state == State.SERVER_EXPECT_CLIENT_HELLO:
            self._server_handle_hello(input_data, output_buf)
        else:
            raise Exception('unhandled state')

    def _client_send_hello(self, output_buf):
        hello = ClientHello(
            random=self.client_random,
            session_id=self.session_id,
            cipher_suites=[
                CipherSuite.AES_128_GCM_SHA256,
            ],
            compression_methods=[
                CompressionMethod.NULL,
            ],

            key_exchange_modes=[
                KeyExchangeMode.PSK_DHE_KE,
            ],
            key_share=[
                (
                    Group.SECP256R1,
                    self.private_key.public_key().public_bytes(
                        Encoding.X962, PublicFormat.UncompressedPoint),
                )
            ],
            signature_algorithms=[
                SignatureAlgorithm.RSA_PSS_RSAE_SHA256,
                SignatureAlgorithm.ECDSA_SECP256R1_SHA256,
                SignatureAlgorithm.RSA_PKCS1_SHA256,
                SignatureAlgorithm.RSA_PKCS1_SHA1,
            ],
            supported_groups=[
                Group.SECP256R1,
            ],
            supported_versions=[
                TLS_VERSION_1_3,
                TLS_VERSION_1_3_DRAFT_28,
                TLS_VERSION_1_3_DRAFT_27,
                TLS_VERSION_1_3_DRAFT_26,
            ]
        )

        self.key_schedule = KeySchedule()
        self.key_schedule.extract(None)

        hash_start = output_buf.tell()
        push_client_hello(output_buf, hello)
        self.key_schedule.update_hash(output_buf.data_slice(hash_start, output_buf.tell()))

        self.state = State.CLIENT_EXPECT_SERVER_HELLO

    def _client_handle_hello(self, data, output_buf):
        buf = Buffer(data=data)
        peer_hello = pull_server_hello(buf)
        assert buf.eof()

        peer_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), peer_hello.key_share[1])
        shared_key = self.private_key.exchange(ec.ECDH(), peer_public_key)

        self.key_schedule.update_hash(data)
        self.key_schedule.extract(shared_key)

        self._setup_traffic_protection(Direction.DECRYPT, Epoch.HANDSHAKE, b's hs traffic')
        self._setup_traffic_protection(Direction.ENCRYPT, Epoch.HANDSHAKE, b'c hs traffic')

        self.state = State.CLIENT_EXPECT_ENCRYPTED_EXTENSIONS

    def _server_handle_hello(self, data, output_buf):
        buf = Buffer(data=data)
        peer_hello = pull_client_hello(buf)
        assert buf.eof()

        self.client_random = peer_hello.random
        self.server_random = os.urandom(32)
        self.session_id = peer_hello.session_id
        self.private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())

        self.key_schedule = KeySchedule()
        self.key_schedule.extract(None)
        self.key_schedule.update_hash(data)

        peer_public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), peer_hello.key_share[0][1])
        shared_key = self.private_key.exchange(ec.ECDH(), peer_public_key)

        # send reply
        hello = ServerHello(
            random=self.server_random,
            session_id=self.session_id,
            cipher_suite=CipherSuite.AES_128_GCM_SHA256,
            compression_method=CompressionMethod.NULL,

            key_share=(
                Group.SECP256R1,
                self.private_key.public_key().public_bytes(
                    Encoding.X962, PublicFormat.UncompressedPoint),
            ),
            supported_version=TLS_VERSION_1_3,
        )

        hash_start = output_buf.tell()
        push_server_hello(output_buf, hello)
        self.key_schedule.update_hash(output_buf.data_slice(hash_start, output_buf.tell()))
        self.key_schedule.extract(shared_key)

        self._setup_traffic_protection(Direction.ENCRYPT, Epoch.HANDSHAKE, b's hs traffic')
        self._setup_traffic_protection(Direction.DECRYPT, Epoch.HANDSHAKE, b'c hs traffic')

        self.state = State.SERVER_EXPECT_FINISHED

    def _setup_traffic_protection(self, direction, epoch, label):
        key = self.key_schedule.derive_secret(label)

        if direction == Direction.ENCRYPT:
            self.enc_key = key
        else:
            self.dec_key = key

        self.update_traffic_key_cb(direction, epoch, key)