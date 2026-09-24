<?php
require __DIR__.'/support/bridge-wordpress-fixture.php';
use KosmosBridge\Options;
use KosmosBridge\Registration\OptionStore;
use KosmosBridge\Registration\SecretStore;

check(SecretStore::ensure_identity(), 'Fresh setup');
$original=SecretStore::get_identity();
check($original['domain']==='template.example', 'Domain stored');
check(strlen($original['secret'])===64, 'Random secret');
check(SecretStore::ensure_identity(), 'Repeated boot');
check(SecretStore::get_identity()===$original, 'Stable boot identity');
$domain='http://www.template.example/';
SecretStore::ensure_identity();
check(SecretStore::get_identity()===$original, 'Scheme/www do not rotate');
$template=$wpdb->rows;
$domain='https://copy.example/';
update_option(Options::LAST_SUCCESS_AT,'yesterday');
SecretStore::ensure_identity();
$copy=SecretStore::get_identity();
check($copy['uuid']!==$original['uuid'], 'Copy gets own UUID');
check($copy['secret']!==$original['secret'], 'Copy gets own key');
check(Options::get_last_success_at()==='', 'Copied success ignored');
$wpdb->rows=$template;
$domain='https://second-copy.example/';
SecretStore::ensure_identity();
check(Options::get_site_uuid()!==$copy['uuid'], 'Independent second copy');
check(Options::get_site_secret()!==$copy['secret'], 'Independent second key');
$wpdb->rows=$template;
$domain='https://template.example/';
check(SecretStore::ensure_identity(), 'Original survives');
check(SecretStore::get_identity()===$original, 'Original unaffected');

reset_fixture(array(Options::SITE_UUID=>'legacy',Options::SITE_SECRET=>'old-key',Options::LAST_SUCCESS_AT=>'yesterday'));
// B caches a missing identity/lock before A migrates using the real Options API.
get_option(Options::IDENTITY,array());
get_option(SecretStore::IDENTITY_LOCK);
$request_b=$cache;
$cache=array();
check(SecretStore::ensure_identity(), 'A migrates legacy identity');
check(Options::get_site_uuid()==='legacy', 'Migration keeps UUID');
check(Options::get_site_secret()==='old-key', 'Migration keeps key');
check(Options::get_last_success_at()==='', 'Legacy unbound success not trusted');
check(OptionStore::read(Options::SITE_SECRET)==='old-key', 'Legacy key remains for in-flight older plugin requests');
$migrated=SecretStore::get_identity();
$cache=$request_b;
check(SecretStore::ensure_identity(), 'B resumes with stale negative cache');
check(SecretStore::get_identity()===$migrated, 'Regression: stale notoptions cannot replace identity');
wp_cache_set(Options::IDENTITY,array('uuid'=>'stale','secret'=>'stale','domain'=>'other.example'),'options');
check(SecretStore::ensure_identity(), 'Stale positive cache is harmless');
check(SecretStore::get_identity()===$migrated, 'Stale positive cache cannot rotate credentials');

// Verify that the harness reproduces the old add_option upsert, not a fictitious lock.
$cache=array();
get_option(SecretStore::IDENTITY_LOCK);
$request_b=$cache;
$cache=array();
$a=array('token'=>'A','expires'=>time()+120);
$b=array('token'=>'B','expires'=>time()+120);
check(add_option(SecretStore::IDENTITY_LOCK,$a,'',false), 'Core inserts A lease');
$cache=$request_b;
check(add_option(SecretStore::IDENTITY_LOCK,$b,'',false), 'Core add_option upsert reproduces unsafe old lock');
check(OptionStore::read(SecretStore::IDENTITY_LOCK)===$b, 'Fixture proves old lock was overwritten');
OptionStore::remove(SecretStore::IDENTITY_LOCK,$b);

check(OptionStore::replace(SecretStore::IDENTITY_LOCK,null,$a), 'New lock inserts A lease');
check(!OptionStore::replace(SecretStore::IDENTITY_LOCK,null,$b), 'Concurrent insert cannot steal lease');
check(!OptionStore::remove(SecretStore::IDENTITY_LOCK,$b), 'Wrong owner cannot release lease');
check(OptionStore::read(SecretStore::IDENTITY_LOCK)===$a, 'Lease remains owned by A');
$domain='https://copy.example/';
check(!SecretStore::ensure_identity(), 'Busy identity lock fails closed');
check(SecretStore::get_identity()===array(), 'Credentials for wrong domain are never exposed');
check(OptionStore::read(Options::IDENTITY)===$migrated, 'Busy lock does not mutate identity');
$expired=array('token'=>'expired','expires'=>time()-1);
check(OptionStore::replace(SecretStore::IDENTITY_LOCK,$a,$expired), 'Lease expiry fixture');
check(SecretStore::ensure_identity(), 'Expired lease safely reclaimed');
$rotated=SecretStore::get_identity();
check($rotated['uuid']!=='legacy' && $rotated['secret']!=='old-key', 'Domain change rotates whole pair');
check(OptionStore::read(Options::SITE_SECRET)===null, 'Rotated identity removes obsolete legacy credentials');
check(OptionStore::read(SecretStore::IDENTITY_LOCK)===null, 'Owned lease released');
OptionStore::replace(SecretStore::IDENTITY_LOCK,null,$b);
check(!OptionStore::remove(SecretStore::IDENTITY_LOCK,$expired), 'Late owner cannot remove successor lease');
check(OptionStore::read(SecretStore::IDENTITY_LOCK)===$b, 'Successor lease preserved');
OptionStore::remove(SecretStore::IDENTITY_LOCK,$b);

// A concurrent writer between read and CAS cannot be overwritten.
$domain='https://another-copy.example/';
$wpdb->before_query=function() use ($a) { global $wpdb; $wpdb->rows[SecretStore::IDENTITY_LOCK]=array('option_value'=>serialize($a)); };
check(!SecretStore::ensure_identity(), 'Concurrent lock insertion blocks rotation');
check(OptionStore::read(Options::IDENTITY)===$rotated, 'Concurrent lock insertion preserves pair');
OptionStore::remove(SecretStore::IDENTITY_LOCK,$a);
check(!OptionStore::replace(Options::IDENTITY,$migrated,$original), 'Stale identity CAS rejected');
check(OptionStore::read(Options::IDENTITY)===$rotated, 'Newer identity not overwritten');

$wpdb->fail_reads=true;
$before=$wpdb->rows;
check(!SecretStore::ensure_identity(), 'Database read failure fails closed');
check(SecretStore::get_identity()===array(), 'No cached credentials used after database failure');
check($wpdb->rows===$before, 'Read failure cannot generate replacement credentials');
$wpdb->fail_reads=false;
$wpdb->fail_writes=true;
check(!SecretStore::ensure_identity(), 'Database write failure fails closed');
check($wpdb->rows===$before, 'Write failure leaves identity untouched');
$wpdb->fail_writes=false;
foreach (array(array(Options::SITE_UUID=>'only-uuid'),array(Options::SITE_SECRET=>'only-secret'),array(Options::IDENTITY=>array('uuid'=>'broken'))) as $broken) {
    reset_fixture($broken);
    $before=$wpdb->rows;
    check(!SecretStore::ensure_identity(), 'Partial/corrupt identity fails closed');
    check($wpdb->rows===$before, 'Partial/corrupt credentials never silently replaced');
}

reset_fixture();
for ($i=0;$i<12;$i++) {
    $domain='https://copy'.$i.'.example/';
    check(SecretStore::ensure_identity(), 'Independent domain transition');
}
$events=OptionStore::read(SecretStore::IDENTITY_EVENTS);
check(count($events)===10, 'Identity event history is bounded');
check(strpos(json_encode($events),'secret')===false, 'Identity history has no secret fields');
check(strpos(json_encode($events),Options::get_site_secret())===false, 'Identity history never logs key');
check(end($events)['reason']==='domain-change', 'Identity reason recorded');
echo "Bridge identity contracts (real WordPress Options API): $checks passed\n";
