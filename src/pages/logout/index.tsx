import React from 'react';
const Logout = () => {
  React.useEffect(() => {
    localStorage.removeItem('Authorization');
    window.location.href = '/';
  }, []);

  return (
    <div>
    </div>
  );
};

export default Logout;