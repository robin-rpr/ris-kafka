from kazoo.client import KazooClient, KazooState
from websockets.asyncio.client import connect
from datetime import datetime, timedelta
from confluent_kafka import Producer
from kazoo.recipe.lock import Lock
from libs.bmp import BMPv3
import rocksdbpy
import ipaddress
import logging
import asyncio
import socket
import signal
import json
import os

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
QUEUE_SIZE = int(os.getenv("RRC_QUEUE_SIZE", 100000))
BACKUP_SIZE = int(os.getenv("RRC_BACKUP_SIZE", 1000000))
BATCH_SIZE = int(os.getenv("RRC_BATCH_SIZE", 1000))
ZOOKEEPER_CONNECT = os.getenv("RRC_ZOOKEEPER_CONNECT")
KAFKA_CONNECT = os.getenv("RRC_KAFKA_CONNECT")
KAFKA_USERNAME = os.getenv("RRC_KAFKA_USERNAME")
KAFKA_PASSWORD = os.getenv("RRC_KAFKA_PASSWORD")
LOG_LEVEL = os.getenv("RRC_LOG_LEVEL", "INFO").upper()
RIS_HOST = os.getenv("RRC_HOST").lower()

# Logger
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Globals
receive_counter = 0  # To store received data size in bytes
send_counter = 0  # To store sent data size in bytes
batch_counter = 0 # To store batch counter
is_leader = False # Leader Lock (Leader Election)
is_failover = False # Failover Lock (Failover Mode)
is_waiting = True # Waiting Lock (Waiting for data)
time_lag = timedelta(0) # Time lag in seconds

# Leader Task
async def leader_task():
    global is_leader
    """
    A single task that continuously checks if we are the leader.
    """
    def _on_state_change(state):
        global is_leader
        """
        Listener to Zookeeper session state changes.
        """
        if state == KazooState.LOST:
            is_leader = False
            raise Exception("Zookeeper session lost. Leadership revoked.")
        elif state == KazooState.SUSPENDED:
            is_leader = False
            logger.warning("Zookeeper connection suspended.")
        elif state == KazooState.CONNECTED:
            logger.info("Zookeeper connected.")
    
    try:
        zk = KazooClient(hosts=ZOOKEEPER_CONNECT)
        zk.add_listener(_on_state_change)
        zk.start()
        lock = Lock(zk, f'collectors/{RIS_HOST}')

        while True:
            # Acquire leadership and register callback on loss
            acquired = await asyncio.to_thread(lock.acquire, blocking=False)
            if acquired:
                is_leader = True
                logger.info("Acquired leadership")
                await asyncio.Event().wait() # Keep the lock until cancellation
            else:
                # Wait until we are the leader
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        try:
            is_leader = False
            lock.release() # Release the lock
            logger.info("Relinquished leadership")
        except Exception as e:
            logger.error(f"Failed to release leadership: {e}")
        raise

# Consumer Task
async def consumer_task(queue, backup):
    """
    A single task that continuously consumes messages from the websocket.

    Args:
        queue (asyncio.Queue): The queue.
        backup (asyncio.Queue): The backup queue.
    """
    global receive_counter
    global is_failover
    global is_waiting
    global is_leader
    try:
        async with connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}") as ws:
            await ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST, "socketOptions": {"includeRaw": True}}}))
            batch = []
            
            async for data in ws:
                receive_counter += len(data)
                marshal = json.loads(data)['data']

                if marshal['type'] == "ris_error":
                    raise Exception(f"Websocket error: {marshal['message']}")

                # Allow only specific messages
                # TODO: Implement also "STATE" and "RIS_PEER_STATE"
                #       Blocked by: https://fosstodon.org/@robinroeper/113909690169940714
                if marshal['type'] not in ["UPDATE", "OPEN", "NOTIFICATION", "KEEPALIVE"]:
                    continue

                # Add message to buffer
                batch.append(marshal)

                # Sort and reset batch
                if len(batch) == BATCH_SIZE:
                    # Sort Messages                   
                    ordered = sorted(batch, key=lambda item: (
                        int(ipaddress.ip_address(item["peer"])), # Group by peer (same peer together)
                        item["timestamp"], # Within each peer, sort by timestamp (earliest first)
                        int(item["id"].split('-')[-1], 16)  # If multiple messages have the same timestamp, order by sequence_id
                    ))
                    # Queue Messages
                    if is_leader:
                        # If we have a backup, use it
                        if backup.qsize() > 0:
                            is_failover = True
                            logger.critical("Failover triggered...")
                            while not backup.empty():
                                item = await backup.get()
                                await queue.put(item)
                            logger.warning("Failover completed")
                            is_failover = False
                            is_waiting = False
                            
                            # Prune backup queue back to normal size
                            while backup.qsize() > BACKUP_SIZE:
                                await backup.get()
                        else:
                            is_failover = False
                            is_waiting = False

                        for item in ordered:
                            if queue.full():
                                raise Exception("Unable to keep up with incoming data")

                            await queue.put(item)
                    else:
                        # If we are not the leader, add items to backup
                        for item in ordered:
                            if not is_failover and backup.full():
                                # Discard oldest message only if not in failover mode
                                await backup.get()
                            await backup.put(item)
                    batch = []
    except asyncio.CancelledError:
        raise
                
# Sender Task
async def sender_task(producer, queue):
    global send_counter
    global is_failover
    global is_leader
    global time_lag
    """
    A single task that continuously sends messages to Kafka.

    Args:
        producer (confluent_kafka.Producer): The Kafka producer.
        queue (asyncio.Queue): The message queue.
    """
    try:
        latest = None
        initialized = False
        produced_size = 0
        reported_size = 0
        batch_size = 0
        seeked = False
        item = None
        db = None

        # Delivery Report Callback
        # NOTE: Delivery reports may arrive out of order
        def delivery_report(err, msg):
            nonlocal reported_size
            if err is None:
                reported_size += 1
            else:
                if err.code() == 3: # UNKNOWN_TOPIC_OR_PART
                    logger.warning(f"Topic not ready, retrying message delivery: {err}")
                    try:
                        # Retry the message delivery
                        producer.produce(
                            topic=msg.topic(),
                            key=msg.key(),
                            value=msg.value(),
                            timestamp=msg.timestamp(),
                            callback=delivery_report
                        )
                        # Poll to ensure the message is queued
                        producer.poll(0)
                    except Exception as e:
                        logger.error(f"Failed to retry message delivery: {e}")
                        raise Exception(f"Message delivery failed: {err}")
                else:
                    raise Exception(f"Message delivery failed: {err}")

        while True:
            if is_leader:
                # Open RocksDB
                if db is None:
                    try:
                        db = rocksdbpy.open_default("/var/lib/rocksdb")
                    except Exception as e:
                        logger.warning(f"Failed to open RocksDB. Delaying by 2000ms")
                        await asyncio.sleep(2)
                        db = None # Reset
                        continue

                # Get details about the last batch
                batch_id = db.get(b'batch_id')

                # If we lost messages since the last execution
                if not initialized:
                    initialized = True
                    if db.get(b'batch_reported') == b'\x00':
                        if db.get(b'batch_transacting') == b'\x01':
                            # Awaiting in-flight transaction (Softlock)
                            raise Exception("Awaiting in-flight transaction")
                        else:
                            # Process deadlocked
                            db.set(b'hardlock', b'\x01')
                            raise Exception("Lost continuity")

                # If we need to seek
                if batch_id is not None and not seeked:
                    try:
                        while True:
                            item = queue.get_nowait()
                            if item['id'] == batch_id.decode('utf-8'):
                                seeked = True
                                break
                    except asyncio.QueueEmpty:
                        if is_failover:
                            logger.warning("Backup message processing is falling behind. Delaying by 200ms")
                            await asyncio.sleep(0.20)
                            continue
                        elif is_waiting:
                            # If we are waiting for data, delay by 1000ms
                            await asyncio.sleep(1)
                            continue
                        else:
                            # Resume failed due to lost continuity (Hardlock)
                            # NOTE: If you encounter this, it means that the stream was interrupted and messages were irreversibly lost.
                            #       You may need to increase your replica count for a more resilient failover or increase system resources.
                            #       The only way to restart again is to delete all data (including your Kafka topics) and restart the service.
                            db.set(b'hardlock', b'\x01')
                            raise Exception(f"Suspended due to failed stream recovery")
                    except Exception as e:
                        raise e
                else:
                    seeked = True

                # If not all messages have been reported
                if batch_size == BATCH_SIZE and reported_size < produced_size:
                    logger.warning("Kafka message processing is falling behind. Delaying by 200ms")
                    await asyncio.sleep(0.20)
                    producer.flush()
                    continue

                # If all messages have been reported
                if batch_size == BATCH_SIZE and reported_size == produced_size:
                    db.set(b"batch_id", item['id'].encode('utf-8'))
                    db.set(b"batch_reported", b'\x01')
                    reported_size = 0
                    produced_size = 0
                    batch_size = 0

                # Await next item in the queue
                item = await queue.get()

                # Whether message is metadata
                is_metadata = False

                # Construct the BMP message
                match item['type']:
                    case 'UPDATE':
                        message = BMPv3.monitoring_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            bgp_update=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                    case 'OPEN':
                        message = BMPv3.peer_up_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            bgp_open=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                    case 'NOTIFICATION':
                        message = BMPv3.peer_down_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            reason_code=3, # Remote system closed
                            bgp_notification=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                    case 'KEEPALIVE':
                        message = BMPv3.keepalive_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            bgp_keepalive=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                    case 'RIS_PEER_STATE':
                        # Internal state of the peer
                        # NOTE: This is not a BGP state!
                        is_metadata = True
                        if item['state'] == 'connected':
                            message = BMPv3.peer_up_message(
                                peer_ip=item['peer'],
                                peer_asn=int(item['peer_asn']),
                                timestamp=item['timestamp'],
                                bgp_open=bytes.fromhex(item['raw']),
                                collector=RIS_HOST
                            )
                        elif item['state'] == 'down':
                            message = BMPv3.peer_down_message(
                                peer_ip=item['peer'],
                                peer_asn=int(item['peer_asn']),
                                timestamp=item['timestamp'],
                                reason_code=5, # Peer monitoring stopped
                                bgp_notification=bytes.fromhex(item['raw']),
                                collector=RIS_HOST
                            )
                    case 'STATE':
                        # Unknown message type
                        # Read more: https://fosstodon.org/@robinroeper/113909690169940714
                        continue
                    case _:
                        raise Exception(f"Unexpected type: {item['type']}")
                    
                # Set transacting to true
                db.set(b"batch_transacting", b'\x01')

                # Send messages to Kafka
                producer.produce(
                    topic=f"{RIS_HOST}.{item['peer_asn'] if not is_metadata else 'meta'}.bmp_raw",
                    key=item['id'],
                    value=message,
                    timestamp=int(item['timestamp'] * 1000),
                    callback=delivery_report
                )

                # Set latest delivered item
                latest = item

                # Increment counter for batch
                batch_size += 1

                # Set last delivered to false
                db.set(b"batch_reported", b'\x00')

                # Increment counter for produced
                produced_size += 1

                # Set transacting to false
                db.set(b"batch_transacting", b'\x00')

                # Update the approximated time lag
                time_lag = datetime.now() - datetime.fromtimestamp(int(item['timestamp']))

                # Increment sent data size
                send_counter += len(message)

                # Poll Kafka producer
                producer.poll(0)
            else:
                # Wait until we are the leader
                await asyncio.sleep(1)
    except asyncio.CancelledError:
        if db is not None:
            # If we are interrupted amid an active message delivery transaction
            if not db.get(b"batch_transacting") == b'\x01':
                # Ensure all messages got delivered
                while db.get(b"batch_reported") == b'\x00':
                    # If not all messages have been reported
                    if reported_size < produced_size:
                        logger.warning("Messages still in queue or transit. Delaying shutdown by 1000ms")
                        await asyncio.sleep(1)
                        producer.flush()
                        continue

                    # If all messages have been reported
                    if reported_size == produced_size:
                        logger.info("Outstanding messages delivered")
                        db.set(b"batch_id", latest['id'].encode('utf-8'))
                        db.set(b"batch_reported", b'\x01')
            # Close RocksDB
            db.close()
        raise

# Logging Task
async def logging_task(queue, backup):
    global receive_counter
    global send_counter
    global is_leader
    global time_lag

    """
    A single task that continuously logs the current state of the system.
    """
    try:
        while True:
            timeout = 10 # seconds

            # Calculate kbps
            received_kbps = (receive_counter * 8) / 1024 / timeout  
            sent_kbps = (send_counter * 8) / 1024 / timeout

            # Calculate time lag
            h, remainder = divmod(time_lag.total_seconds(), 3600)
            m, s = divmod(remainder, 60)

            # Log out
            logger.info(f"host={RIS_HOST} leader={is_leader} receive={received_kbps:.2f} kbps send={sent_kbps:.2f} kbps lag={int(h)}h {int(m)}m {int(s)}s backup={backup.qsize()} queue={queue.qsize()}")

            # Reset counters
            receive_counter = 0 
            send_counter = 0

            await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        raise

# Signal handler
def handle_shutdown(signum, frame, shutdown_event):
    """
    Signal handler for shutdown.

    Args:
        signum (int): The signal number.
        frame (frame): The signal frame.
        shutdown_event (asyncio.Event): The shutdown event.
    """
    logger.info(f"Signal {signum}. Triggering shutdown...")
    shutdown_event.set()

# Main Coroutine
async def main():
    # Tasks
    tasks = []

    logger.info("Starting up...")

    try:
        # Kafka Producer
        producer = Producer({
            'bootstrap.servers': KAFKA_CONNECT,
            'enable.idempotence': True,
            'acks': 'all',
            'retries': 10,
            'compression.type': 'lz4',
            'security.protocol': 'SASL_PLAINTEXT',
            'sasl.mechanism': 'PLAIN',
            'sasl.username': KAFKA_USERNAME,
            'sasl.password': KAFKA_PASSWORD,
        })

        # Wait for Kafka to be ready
        logger.info("Waiting for Kafka connection...")
        max_retries = 30  # 30 retries with exponential backoff
        retry_count = 0
        while retry_count < max_retries:
            try:
                # Try to get metadata to verify connection
                producer.list_topics(timeout=10)
                logger.info("Successfully connected to Kafka")
                break
            except Exception as e:
                retry_count += 1
                if retry_count == max_retries:
                    raise Exception(f"Failed to connect to Kafka after {max_retries} attempts: {str(e)}")
                wait_time = min(2 ** retry_count, 30)  # Exponential backoff with 30s max
                logger.warning(f"Kafka not ready, retrying in {wait_time} seconds... (Attempt {retry_count}/{max_retries})")
                await asyncio.sleep(wait_time)

        # Queues
        queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        backup = asyncio.Queue(maxsize=BACKUP_SIZE)

        # Create an event to signal shutdown
        shutdown_event = asyncio.Event()

        # Register SIGTERM handler
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown, signal.SIGTERM, None, shutdown_event)
        loop.add_signal_handler(signal.SIGINT, handle_shutdown, signal.SIGINT, None, shutdown_event)  # Handle Ctrl+C

        # Start leader task
        tasks.append(asyncio.create_task(leader_task()))

        # Start the consumer and sender tasks
        tasks.append(asyncio.create_task(consumer_task(queue, backup)))
        tasks.append(asyncio.create_task(sender_task(producer, queue)))

        # Start the logging task
        tasks.append(asyncio.create_task(logging_task(queue, backup)))

        # Monitor tasks for exceptions
        for task in tasks:
            task.add_done_callback(lambda t: shutdown_event.set() if t.exception() else None)

        # Wait for the shutdown event
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")

        # Cancel all running tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
