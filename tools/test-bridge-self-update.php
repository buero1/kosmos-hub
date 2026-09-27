<?php
// Use real released classes already in memory, then load registration code from the new package.
require __DIR__.'/support/bridge-wordpress-fixture.php';
use KosmosBridge\Options;
use KosmosBridge\Plugin;
use KosmosBridge\Registration\SecretStore;

check((bool)getenv('BRIDGE_PRELOADED_DIR'), 'Old release directory is required');
check(in_array(Options::BRIDGE_VERSION, array('0.3.68', '0.3.69'), true), 'Exercise a released old runtime');
$identity = array('uuid'=>'existing-site', 'secret'=>'existing-key', 'domain'=>'template.example');
reset_fixture(array(Options::IDENTITY=>$identity));
$before = $wpdb->rows;

// Mirrors the activation callback executed by the old updater after replacing its own files.
Plugin::activate();
check(isset($scheduled[Plugin::REGISTER_HOOK]), 'Registration is deferred to a fresh request');
check(isset($scheduled[Plugin::HEARTBEAT_HOOK]), 'Heartbeat remains scheduled');
check(!$requests, 'Mixed-version activation sends no registration');
check($wpdb->rows === $before, 'Mixed-version activation neither rotates keys nor changes registration state');
check(Plugin::run_registration() === false, 'Repeated registration in old request remains deferred');
check(!$requests && $wpdb->rows === $before, 'Repeated call remains side-effect free');
echo 'Bridge self-update from '.Options::BRIDGE_VERSION.": $checks checks passed\n";
