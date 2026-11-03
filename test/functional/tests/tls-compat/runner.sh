#!/bin/bash -x

set -e

pushd /tests/tls-compat/

openssl genrsa -out root.key 2048
openssl req -new -key root.key -out root.csr -nodes -subj "/CN=odyssey-test-cn"
openssl x509 -req -days 2 -in root.csr -signkey root.key -out root.pem

openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -nodes -subj "/CN=localhost"
openssl x509 -req -in server.csr -CA root.pem -CAkey root.key -CAcreateserial -out server.pem -days 2

openssl genrsa -out auth_query_user_md5.key 2048
openssl req -new -key auth_query_user_md5.key -out auth_query_user_md5.csr -nodes -subj "/CN=auth_query_user_md5"
openssl x509 -req -in auth_query_user_md5.csr -CA root.pem -CAkey root.key -CAcreateserial -out auth_query_user_md5.pem -days 2

popd

echo "ssl packages:"
dpkg -l | grep ssl | cat

/usr/bin/odyssey /tests/tls-compat/config.conf
sleep 1

psql 'host=localhost port=6432 user=postgres dbname=postgres sslmode=disable' && {
    echo "should not connect with ssl mode require"
    exit 1
}
psql 'host=localhost port=9432 user=postgres dbname=postgres sslmode=disable' && {
    echo "should not connect with ssl mode verify-ca"
    exit 1
}
psql 'host=localhost port=10432 user=postgres dbname=postgres sslmode=disable' && {
    echo "should not connect with ssl mode verify-full"
    exit 1
}

# prefer and allow at listen section should accept both tls and non-tls connections
psql 'host=localhost port=7432 user=postgres dbname=postgres sslmode=disable' || exit 1
psql 'host=localhost port=7432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem' || exit 1
psql 'host=localhost port=8432 user=postgres dbname=postgres sslmode=disable' || exit 1
psql 'host=localhost port=8432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem' || exit 1

# direct tls (sslnegotiation=direct) skips SSLRequest and requires the postgresql ALPN
psql 'host=localhost port=6432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem sslnegotiation=direct' -c 'select 1' || exit 1
psql 'host=localhost port=7432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem sslnegotiation=direct' -c 'select 1' || exit 1
openssl s_client -connect 127.0.0.1:6432 -alpn http/1.1 </dev/null && {
    echo "should reject direct tls with unknown alpn"
    exit 1
}
openssl s_client -connect 127.0.0.1:6432 </dev/null || exit 1
sleep 1
grep 'received direct SSL connection request without ALPN protocol negotiation extension' /var/log/odyssey.log || {
    echo "should reject direct tls without alpn"
    exit 1
}

# Check some read-only load will work with tls
pgbench 'host=localhost port=6432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem' -j 2 -c 10 --select-only --no-vacuum --progress 1 -T 20 || exit 1

pgbench 'host=localhost port=6432 user=postgres dbname=postgres sslmode=verify-full sslrootcert=/tests/tls-compat/root.pem' -j 2 -c 10 --select-only --no-vacuum --progress 1 -T 20 --connect || exit 1

sleep 1

ody-stop || exit 1
