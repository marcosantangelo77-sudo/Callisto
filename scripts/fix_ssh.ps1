$c = Get-Content C:\ProgramData\ssh\sshd_config
$o = @()
foreach ($l in $c) {
    if ($l -match 'AuthorizedKeysFile.*administrators') {
        $o += '#' + $l
    }
    elseif ($l -eq '#PasswordAuthentication yes') {
        $o += 'PasswordAuthentication yes'
    }
    else {
        $o += $l
    }
}
Set-Content C:\ProgramData\ssh\sshd_config $o
Restart-Service sshd
echo "FIXED - SSH password auth enabled"
